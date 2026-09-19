# gphotos-sync

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
- **Scope/verification.** This uses the full `photoslibrary` scope. For
  personal use, keep the OAuth consent screen in "Testing" mode with
  yourself as a test user — no Google app review is required. If you
  later want other people to use this, the app would need Google's
  verification process.
