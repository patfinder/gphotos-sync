# gphotos-sync

Three tools:

- `gphotos_browser.py` works on Google Photos **directly** through
  photos.google.com in a browser (no export needed). It lists, downloads
  and optionally trashes photos/videos that are in no album and newer than
  a date.
- `takeout_sync.py` does the same from a Google Takeout export.
- `gphotos_sync.py` syncs a local folder with an album this app created
  (see its limitations below).

## Directly from Google Photos, via the browser (`gphotos_browser.py`)

```bash
pip install playwright        # plus `playwright install chromium` if you have no Google Chrome

python gphotos_browser.py login       # sign in once in the Chrome window; kept in .gphotos_browser_profile/
python gphotos_browser.py probe       # read-only check that it understands the site

# List items in no album, taken on/after 1 Nov 2026 (read-only)
python gphotos_browser.py list --since 2026-11-01
python gphotos_browser.py list --since 2026-11-01 --csv > unalbumed.csv

# Download the originals into ~/Pictures/unalbumed/YYYY/MM/
python gphotos_browser.py download --since 2026-11-01 --dest ~/Pictures/unalbumed

# ...and move each one to Google Photos Trash after its download is verified
python gphotos_browser.py download --since 2026-11-01 --dest ~/Pictures/unalbumed --delete --dry-run
python gphotos_browser.py download --since 2026-11-01 --dest ~/Pictures/unalbumed --delete
```

How it works:

1. It opens every album (yours and shared ones; `--no-shared` ignores
   shared albums) and records the ID of each item in it.
2. It scrolls your library from newest to oldest, reading each item's ID,
   type and date, and stops once it passes `--since`.
3. The result is the items from step 2 that aren't in any album.

Deletion uses the exact item it downloaded, by ID, so there's no filename
guessing. Before trashing, it also checks that the downloaded file is intact
(SHA-256) and that the photo's info panel doesn't list an album, and it asks
for each one unless you pass `--yes`. Progress is kept in
`<dest>/.gphotos_browser_state.jsonl`, so re-runs skip what's already done.

It needs Google Chrome (or Chromium) installed; set `GPHOTOS_CHROME` to
its path if it isn't found. Close any Chrome window still using
`.gphotos_browser_profile/` before running.

**Caveats:**

- This depends on how photos.google.com is laid out. It has been tested
  only against a mock of the site, so run `probe` first. If it reports
  `NO` anywhere, the page assumptions at the top of `gphotos_web.py` need
  adjusting. `probe --dump page.html` saves a page to help with that.
- Scanning opens every album, so a library with many large albums takes a
  while.
- It needs an English Google Photos UI, because it reads dates from tile
  labels.
- Live Photos are downloaded however Google's own Download serves them.

## From a Takeout export (`takeout_sync.py`)

Google's API can't list your whole library any more (see
`docs/design.md`), so this works from a Takeout export:

1. Go to <https://takeout.google.com>, click **Deselect all**, tick only
   **Google Photos**, choose `.zip` format, and create the export. Download
   all of the `takeout-*.zip` parts.
2. Run the script (it needs only the Python standard library):

```bash
# Preview
python takeout_sync.py --since 2024-06-01 --dest ~/Pictures/unalbumed \
    ~/Downloads/takeout-*.zip --dry-run

# Copy (into ~/Pictures/unalbumed/YYYY/MM/)
python takeout_sync.py --since 2024-06-01 --dest ~/Pictures/unalbumed \
    ~/Downloads/takeout-*.zip
```

To only **list** the matching photos and videos without copying anything
(no `--dest` needed):

```bash
python takeout_sync.py --since 2024-06-01 --list ~/Downloads/takeout-*.zip
# 2024-07-03 05:46  video    12.3 MB  VID_20240703_101010.mp4   (Photos from 2024/VID_20240703_101010.mp4)
# ...
# 812 item(s): 790 photo(s), 22 video(s), 4.1 GB total

# As CSV, for a spreadsheet
python takeout_sync.py --since 2024-06-01 --list --list-format csv \
    ~/Downloads/takeout-*.zip > unalbumed.csv
```

The list is sorted oldest first and shows the filename as Google Photos
displays it. Rows of type `motion` are the video half of a Live Photo or
motion photo (see below). `--until` and `--include-undated` apply here too; undated
items are listed last.

Options: `--until YYYY-MM-DD`, `--layout year-month|year|flat`,
`--include-undated`, `--ignore-folder NAME`, `-v` (lists the album folders
found and the undated files). An extracted Takeout folder works in place
of the zips. Re-running with a newer export only copies what's new.

### Deleting the copied photos from Google Photos (`--delete`)

Add `--delete` to move the copied photos to Google Photos **Trash**, where
they stay restorable for 60 days. Google has no API for deleting photos, so
this drives photos.google.com in a Chrome window using Playwright:

```bash
pip install playwright        # plus `playwright install chromium` if you have no Google Chrome

# Recommended first: find and verify each photo online, but delete nothing
python takeout_sync.py --since 2024-06-01 --dest ~/Pictures/unalbumed \
    ~/Downloads/takeout-*.zip --delete --dry-run

# Delete, confirming each photo (y / N / a = yes to all / q = quit)
python takeout_sync.py --since 2024-06-01 --dest ~/Pictures/unalbumed \
    ~/Downloads/takeout-*.zip --delete
```

On the first run a browser window opens. Sign in to Google there (the
sign-in is kept in `.gphotos_browser_profile/`), then press Enter in the
terminal. For each photo the script:

1. checks that the local copy still exists and matches what was copied
   (SHA-256);
2. searches Google Photos for its original filename and opens each hit;
3. accepts a hit only if its info panel shows that exact filename and the
   capture date (± 1 day for time zones);
4. skips the photo if no hit matches, or if more than one does;
5. skips photos with no known date.

The progress is recorded in `<dest>/.takeout_sync_ledger.jsonl`, so you can
stop at any time and re-run later. `--yes` skips the per-photo prompt.

**Caveats:**

- The selectors match the photos.google.com page layout that was current
  when this was written, and it was only tested against a mock site.
  Always try `--dry-run` first. If Google changes the site, it fails safe:
  photos are reported as not found, nothing is deleted.
- Album membership comes from the Takeout snapshot. If you've added a photo
  to an album since the export, it would still be deleted. Use a fresh
  export.
- The date check assumes the Google Photos UI is in English.

### Videos, Live Photos and motion photos

Videos are handled exactly like photos. iPhone **Live Photos** and Pixel
**motion photos** are one item in Google Photos but two files in Takeout
(`IMG_1.HEIC` + `IMG_1.MOV`, or `PXL_1.MP.jpg` + `PXL_1.MP`), and only the
still has a metadata file. The script pairs the video with its still, so
both get the still's date, the same album decision and the same folder.
With `--delete`, a Live Photo is only trashed once both halves are safely
copied. If it can't tell which still a video belongs to (e.g. `IMG_7.HEIC`
and `IMG_7.JPG` both next to `IMG_7.MOV`), it skips that video and blocks
deletion of both stills.

For a non-English Takeout, the timeline folders aren't named
`Photos from YYYY`. Pass e.g. `--year-folder-regex '^Fotos von \d{4}$'`.

## Album sync (`gphotos_sync.py`)

Sync a local folder with a Google Photos album:

- `download` — pulls album items that aren't in the local folder yet.
- `upload` — pushes local files that aren't in the album yet.
- `sync` — does both, in that order.

Sync is **additive only**: nothing is ever deleted, and existing remote
items are never overwritten. That's a deliberate limitation, not an
oversight — the Google Photos API doesn't support replacing a media
item's bytes or deleting from an album on behalf of the user in a way
that's safe to automate. If you edit a file locally after it's been
uploaded once, it won't be re-uploaded unless you pass `--force`.

## 1. Create OAuth credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create a project (or reuse one).
2. Enable the **Google Photos Library API** for that project.
3. Configure the OAuth consent screen as **External** and **Testing**
   status, and add your own Google account under "Test users". (Testing
   mode gives your own account full scope access without needing Google
   to verify the app.)
4. Create an **OAuth client ID** of type **Desktop app**.
5. Download the JSON and save it as `client_secret.json` in this
   directory (or pass `--client-secret /path/to/file.json`).

## 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Authenticate

```bash
python gphotos_sync.py auth
```

This opens a browser for the OAuth consent flow and stores a refreshable
token in `token.json`.

## 4. Sync

```bash
# Pull an existing album down to a local folder
python gphotos_sync.py download --album "Vacation 2024" --dir ./vacation

# Push local photos/videos up into an album (creating it if needed)
python gphotos_sync.py upload --album "Vacation 2024" --dir ./vacation --create

# Both directions
python gphotos_sync.py sync --album "Vacation 2024" --dir ./vacation --create

# Preview without making changes
python gphotos_sync.py sync --album "Vacation 2024" --dir ./vacation --dry-run
```

Each synced folder gets a `.gphotos_manifest.json` tracking which local
files correspond to which Google Photos media item IDs, so re-running
the command is cheap and idempotent.

## Known limitations

- **You can only upload into albums your app created.** The Photos
  Library API can't add items to an album it didn't create, even if you
  own it. If `upload`/`sync` fails on a pre-existing album, create a new
  one via `--create` instead (or let the script create it on first run).
- **No delete/overwrite sync.** See above — this tool only ever adds
  files, in both directions.
- **Duplicate filenames.** Google Photos allows multiple items with the
  same filename in one album; on download, a colliding name is
  disambiguated by appending part of the media item ID.
- **Scope/verification.** Google removed the full `photoslibrary` scope on
  2025-03-31, so this uses the `appendonly`, `readonly.appcreateddata` and
  `edit.appcreateddata` scopes. The script can only see albums and items it
  created itself; an album made in the Photos app won't be found, and
  `download` only returns items this script uploaded. For personal use, keep the OAuth consent screen in "Testing" mode with
  yourself as a test user — no Google app review is required. If you
  later want other people to use this, the app would need Google's
  verification process.
