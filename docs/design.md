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

### Other choices

- Takeout `.zip` parts are read in place (no extraction needed), and an
  extracted folder works too. An item and its album copy may sit in
  different zip parts, so all inputs are scanned together.
- Output goes into `YYYY/MM/` by default (`--layout year|flat` also
  available). File mtimes are set to the capture time.
- Re-runs are idempotent: a target with identical content is skipped, and
  a different file with the same name gets `_1`, `_2`, ... added. No state
  file is needed.
- Copy-only: nothing in Google Photos or in the export is modified.
