# Design notes

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
