# gphotos-sync

Two tools:

- `takeout_sync.py` copies photos that are **in no album** and were taken
  **after a given date** out of a Google Takeout export.
- `gphotos_sync.py` syncs a local folder with an album this app created
  (see its limitations below).

## Photos not in any album, newer than a date (`takeout_sync.py`)

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

Options: `--until YYYY-MM-DD`, `--layout year-month|year|flat`,
`--include-undated`, `--ignore-folder NAME`, `-v` (lists the album folders
found and the undated files). An extracted Takeout folder works in place
of the zips. Re-running with a newer export only copies what's new.

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
