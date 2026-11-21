# Design notes

## Scheduled backup pipeline (`gphotos_backup.py`)

**Goal (the user's real workflow):** the phone syncs to the MAIN Google
account. On a schedule, unattended: copy MAIN → computer → BACKUP account,
then free up MAIN. Photos the user wants to keep in MAIN go in `_Keep It`.

**Decisions (the user chose each recommended option):**
- **Trash from MAIN** only after the computer copy **and** the
  backup-account copy are verified. The alternatives were trashing after
  the computer copy only, or never deleting.
- **Protection:** any album protects a photo in MAIN, and photos in albums
  other than `_Keep It` are listed for review. The alternatives were
  `_Keep It` plus shared albums, or `_Keep It` only. Deleting a photo from
  a shared album would also remove it for other people.
- **Schedule:** daily at 02:00, via a systemd user timer with
  `Persistent=true`, so a missed run happens at next login. The
  alternatives were every 6 hours, or weekly.
- **Backup organisation:** library only, no albums.

**Which mechanism does what:**
- **MAIN account: browser.** It can't be read through the API any more
  (only app-created items are visible). This reuses `gphotos_web.py`:
  Chrome is started normally and attached over CDP, which fixed Google's
  sign-in block.
- **BACKUP account: the official Library API.** The `appendonly` scope
  still works after the 2025 changes, and the API is far more robust than
  driving a website. `readonly.appcreateddata` confirms each upload with
  `mediaItems.get`. OAuth apps left in "Testing" get refresh tokens that
  expire after 7 days, so the README tells the user to set the consent
  screen to "In production". An unverified app is fine for personal use.

**Selection:** every item taken on/after the configured `since`, found by
scrolling the date-sorted library and stopping at `since`. The user's
suggestion was adopted: check each recent photo, rather than scanning all
25 albums. A fixed `since` instead of a moving cursor means late-synced
photos are still caught. Already-processed items cost only a state lookup
(plus a panel check for photos that stay in MAIN).

**Deciding "in no album"** uses both sources, so either one can protect a
photo:
- (a) the keep album's page: its link is found once by title and cached as
  a path;
- (b) the photo's info panel, which must have **loaded** (its "Details"
  section is visible) and show no "Albums" section. A panel that doesn't
  load is retried once, then the photo is kept.

A self-test before the first deletion of each run requires (b) to detect
the album on up to 3 keep-album photos.

**Unattended behaviour:**
- `ensure_logged_in(interactive=False)` raises `NeedLogin` instead of
  prompting.
- Failures are collected as problems, logged, sent as a `notify-send`
  notification, and give a non-zero exit code, so systemd shows the unit
  as failed.
- An `fcntl` lock prevents overlapping runs.
- With no `DISPLAY`/`WAYLAND_DISPLAY`, Chrome runs headless automatically.
- Downloads that arrive as `.zip` (possible for Live Photos) are unpacked,
  using basenames only so nothing can be written outside the archive.

**State** is append-only JSONL in the archive:
- `downloaded`: per main-account ID → files, SHA-256, size and mtime;
- `uploaded`: per SHA-256 → backup media ID;
- `trashed`;
- `keep_album`;
- `run` summaries.

Daily runs trust unchanged size and mtime, so big files aren't re-read
every night. Right before a deletion, every file is fully re-hashed.

**Testing** used a fake main site (virtual grids, albums, info panel,
downloads, trash dialog, a page that never finishes loading) and a fake
Library API, driven through `main()` with a dummy token. Covered:
- a dry run;
- a full run;
- a failed upload: only that photo stays in MAIN, and the next run
  finishes it;
- a no-op third run;
- a failed self-test: backups happen, nothing is deleted;
- a signed-out main account: exit 2 and a notification;
- a missing backup authorization: nothing deleted;
- the generated unit files (checked with `systemd-analyze verify`, in a
  throwaway home folder).

A bug found this way: `State.add` didn't return its record, so every
download was reported as failed. It was fixed.

The real Google sign-in, the real Library API upload, and the timer on the
user's machine still need the user to try them.


## Downloading photos that are in no album, newer than a date (`takeout_sync.py`)

**Goal:** copy every Google Photos item that is not in any album and was
taken on or after a given date onto the local machine.

### Decision: read a Google Takeout export, not the API

- **Photos Library API (rejected):** since 2025-03-31 the only scopes it
  accepts (`readonly.appcreateddata` and similar) expose just the items the
  calling app uploaded itself. It can no longer list the user's library or
  albums made in the Photos app, so `gphotos_sync.py` can't be extended to
  do this.
- **Photos Picker API (rejected):** the user picks items by hand in a
  Google UI on every run, and the API never reports album membership, so
  "not in any album" can't be computed.
- **rclone / other third-party sync tools (rejected):** they're built on
  the same Library API and hit the same limits.
- **Browser automation of photos.google.com (rejected):** fragile, against
  the ToS spirit, and album membership would mean visiting every album.
- **Google Takeout (chosen):** it's a complete export with album structure
  and capture timestamps. The one manual step is requesting the export
  (Takeout can also be scheduled every 2 months).

### How "not in an album" is computed

Takeout puts every item into a `Photos from YYYY/` folder, and copies items
that belong to an album into a folder named after the album. An item in a
timeline folder counts as being in an album if its **bytes** match a file
in any album folder. Sizes are compared first and SHA-256 only on a size
match, so hashing stays cheap. Matching by content rather than filename
avoids false positives from common names like `IMG_0001.jpg`.
`Trash`, `Bin`, `Archive`, `Locked Folder` and `Failed Videos` don't count
as albums (configurable with `--ignore-folder`).

### Capture date

1. `photoTakenTime` from the JSON sidecar (falls back to `creationTime`).
   Sidecar names differ between Takeout versions and get truncated
   (`x.jpg.json`, `x.jpg.supplemental-metadata.json`,
   `x.jpg.supplemental-me.json`, `x.jpg.supplemental-metadata(1).json`, ...),
   so sidecars are indexed by the `title` field inside them plus the `(n)`
   duplicate counter, not by guessing filenames. `-edited` copies use the
   original's sidecar, and truncated long names fall back to a unique
   prefix match.
2. A date in the filename (`IMG_20240131_...`, `PXL_...`, `2024-01-31 ...`).
3. Otherwise the item counts as undated and is skipped unless
   `--include-undated` is given.

`--since` and `--until` are local-time midnights.

### Deleting from Google Photos after copying (`--delete`, `gphotos_web.py`)

No Google API can delete a library item: the Library API never had a
delete call, and Takeout is read-only. The options considered were:

- **Browser automation with Playwright (chosen):** the only way to delete
  automatically. It's fragile against UI changes, so it's built to fail
  safe.
- **Writing a list for manual deletion (rejected by the user):** safest,
  but manual.
- **Claude driving Chrome once (rejected):** not repeatable.

Takeout gives no Google media-item ID, so a local file has to be matched
back to the online item. The matching works like this:

- Search by the sidecar `title` (the filename Google shows, without
  Takeout's `(n)` suffix).
- Open each hit and require that the info panel shows the exact filename
  on its own line (`IMG_1.jpg` must not match `IMG_1.jpg.bak`) and the
  capture date. The date check allows ±1 day because Google shows the
  photo's own time zone. Several date formats are accepted.
- **Exactly one** hit must match, or the photo is skipped. This prevents
  deleting the wrong one of two same-named photos taken on the same day.
- Undated items are never deleted, because a filename alone is too weak to
  match on.
- Before deleting, the local copy is re-hashed and must still match what
  was copied.
- Deletion is a move to Trash, which can be restored for 60 days. Each one
  is confirmed in the terminal unless `--yes` is given.

`<dest>/.takeout_sync_ledger.jsonl` is an append-only log of `copied` and
`deleted` events (keyed by SHA-256). It makes deletion resumable and
separate from copying: a later run deletes items copied earlier, which is
also how an interrupted session continues.

A persistent browser profile (`.gphotos_browser_profile/`, git-ignored)
keeps the Google sign-in. Real Chrome (`channel="chrome"`) is used when
it's installed, because Google often refuses sign-in from bundled Chromium.

The flow was tested against a local mock of the site. It covered exact
matches, a same-name photo with a different date, a substring name, an
ambiguous pair, a local copy that had changed, y/n/q prompts and
resuming. `GPHOTOS_BASE_URL` and `GPHOTOS_HEADLESS=1` exist for that
testing.

### Live Photos and motion photos

Takeout splits these into a still and a video, and only the still has a
sidecar:

- iPhone: `IMG_1.HEIC` + `IMG_1.MOV`
- Pixel: `PXL_1.MP.jpg` + `PXL_1.MP` (`.MP` was added to the media types)

Online they are one item. Treating them as separate items had two
problems: the video half came out undated and wasn't copied, and `--delete`
would trash the whole item after copying only the still, which is data
loss.

**Pairing** (`pair_motion_parts`): a video with no sidecar of its own is
paired with the still in the same folder whose stem matches. The stem can
be the same (also with `(n)` counters), or the still's stem can be the
video's stem plus `.MP`/`.MV`. Videos that have their own sidecar are real
separate items and are never paired, even when a photo shares their stem.

**Handling a pair** as one unit:

- Both halves take the still's date and Google filename.
- If either half is found in an album folder, the pair counts as in an
  album.
- The ledger records `parts` on the still and `part_of` on the video. Only
  stills are deletion candidates, and trashing one marks its parts deleted
  too.
- Deleting a still requires every part to be in the ledger and to match its
  SHA-256 on disk.

**Ambiguous cases** (two stills with the stem of one video): the video is
left unpaired, so it's undated and skipped by default. It's listed as a
part of **every** candidate still, so none of them can be deleted until it
has been copied (e.g. with `--include-undated`). This is a guess at the
worst case, and it errs on the side of keeping data online.

**Ledger upgrades**: a later `copied` record for the same SHA-256 replaces
the earlier one. A ledger written before parts were tracked therefore gets
the `parts` links on the next run, which always rescans before deleting.

### Browser-only mode (`gphotos_browser.py`)

This was requested as an alternative to Takeout: it needs no export
request and no wait, and it's always current.

**Album membership** comes from IDs, not file contents. The script opens
the `albums` and `sharing` pages, opens every album, and collects each
item's media key from its `/photo/<id>` link. The library grid is then
scrolled newest-first, reading the ID, photo/video type and date from each
tile's label (e.g. `Photo - Landscape - Nov 5, 2026, 12:34:56 PM`).
Scrolling stops at the first tile older than `--since`, since the grid is
sorted by date taken.

- **Virtual grids:** the site only keeps the visible tiles in the page,
  so tiles are collected after every scroll step. Mouse-wheel scrolling
  over the middle of the window is used because it works whatever element
  actually scrolls.
- **Downloads** use Google's own Download (the Shift+D shortcut, with the
  More options menu as a fallback), so files are the originals.
- **Deletion** trashes the same ID that was downloaded, after:
  - verifying the local file's SHA-256;
  - an independent check that the photo's info panel shows no album: no
    "Albums" heading, and no line equal to the name of an album found by
    the scan. Matching on names was added because on the real site the
    heading wasn't detected; names don't depend on how the panel labels
    the section. The heading form also catches an album the scan missed.
    The name form only knows albums the scan found;
  - a **self-test** at the start of every `--delete` run: up to 3 photos
    known to be in albums (from different albums) are opened by their
    library URL, and the check must report an album for each. This proves
    both that album IDs and library IDs are the same item, and that the
    check can see albums. If it fails, `--delete` refuses to run, so
    deletion never relies on IDs alone;
  - a per-item prompt, unless `--yes`.
  Dry runs perform the info-panel check too, so they predict exactly.
- **Launching Chrome:** Google refuses sign-in ("This browser or app may
  not be secure") in a Chrome that Playwright launched itself, because of
  its automation flags. So the script starts Chrome as an ordinary program
  with a separate profile folder (`.gphotos_browser_profile/`) and a
  debugging port bound to 127.0.0.1, then attaches with
  `connect_over_cdp`. The user signs in to their own account by hand in
  that window.
  - A separate profile is required: recent Chrome refuses remote debugging
    on the default profile, and it keeps the script away from the user's
    everyday browser data.
  - On exit Chrome gets SIGTERM, so it saves the sign-in cookies.
  - The user explicitly chose this after the automation-flag sign-in block.
    The alternatives were Takeout only, or Claude in Chrome in their normal
    browser.
- **Assumptions** about the page (selectors, URL patterns, label format)
  are constants at the top of `gphotos_web.py`. `probe` reports how well
  they match the live site.
- **State:** `<dest>/.gphotos_browser_state.jsonl` records downloads (ID
  to local path and SHA-256) and deletions. It's separate from the Takeout
  ledger because items are keyed by ID instead of content hash.
- **Testing:** this was tested only against a local mock that reproduces
  virtualised grids, albums, shared albums, the info panel, downloads and
  the trash dialog. The cases covered were early stopping, same-name files,
  the hidden-album guard, a local copy that had changed, prompts, and
  resuming.

**Trade-offs vs Takeout:** this needs no export and always shows the
current state. It's slower for big albums and depends on the website's
layout, which can change. Takeout's album check (by file contents) doesn't
depend on the site at all.

### Listing only (`--list`)

`--list` uses the same scan and filters as copying, then prints the matches
instead of copying them. It never touches `--dest` or the ledger, and it
can't be combined with `--delete`. It was added as a flag on the same
script, not a separate tool, so that the list always matches exactly what a
copy would select.

- Output is text (default) or CSV (`--list-format csv`). The list goes to
  stdout and status and summary lines go to stderr, so a redirect gives a
  clean file.
- It's sorted by capture date, oldest first, with undated items last.
- Photo or video is decided by file extension.
- The name shown is the one Google Photos displays (the sidecar title),
  with the Takeout path alongside it.

### Other choices

- Takeout `.zip` parts are read in place (no extraction needed), and an
  extracted folder works too. An item and its album copy may sit in
  different zip parts, so all inputs are scanned together.
- Output goes into `YYYY/MM/` by default (`--layout year|flat` also
  available). File mtimes are set to the capture time.
- Re-runs are idempotent: a target with identical content is skipped, and
  a different file with the same name gets `_1`, `_2`, ... added. No state
  file is needed.
- Copy-only unless `--delete` is given. The export itself is never modified.
