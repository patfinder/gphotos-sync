#!/usr/bin/env python3
"""
Sync a local folder with a Google Photos album (upload local-only files,
download remote-only items). Uses the Google Photos Library API directly
over HTTPS (no googleapiclient dependency, since Google dropped the
Photos Library API from the discovery service).

Setup:
    See README.md for creating OAuth credentials.

Usage:
    python gphotos_sync.py auth
    python gphotos_sync.py download --album "Vacation 2024" --dir ./vacation
    python gphotos_sync.py upload   --album "Vacation 2024" --dir ./vacation --create
    python gphotos_sync.py sync     --album "Vacation 2024" --dir ./vacation --create
"""
import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

API_BASE = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL = f"{API_BASE}/uploads"

# Full read/write scope. Works without Google app verification as long as
# the OAuth client is in "Testing" mode and you add your own account as a
# test user (see README.md).
SCOPES = ["https://www.googleapis.com/auth/photoslibrary"]

DEFAULT_CLIENT_SECRET = "client_secret.json"
DEFAULT_TOKEN_FILE = "token.json"
MANIFEST_NAME = ".gphotos_manifest.json"

PAGE_SIZE = 100


# ---------------------------------------------------------------- auth ----

def load_credentials(client_secret_path, token_path):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        if not os.path.exists(client_secret_path):
            sys.exit(
                f"Missing OAuth client secret file: {client_secret_path}\n"
                "Download it from Google Cloud Console (OAuth client, Desktop app "
                "type) and place it there, or pass --client-secret. See README.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    return creds


def auth_headers(creds):
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}"}


# ------------------------------------------------------------- HTTP util --

def api_get(creds, path, params=None):
    resp = requests.get(f"{API_BASE}{path}", headers=auth_headers(creds), params=params)
    _raise_for_status(resp)
    return resp.json()


def api_post(creds, path, body):
    headers = auth_headers(creds)
    headers["Content-Type"] = "application/json"
    resp = requests.post(f"{API_BASE}{path}", headers=headers, data=json.dumps(body))
    _raise_for_status(resp)
    return resp.json()


def _raise_for_status(resp):
    if not resp.ok:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Google Photos API error {resp.status_code}: {detail}")


# ----------------------------------------------------------- albums ------

def find_album(creds, title):
    """Look for an album with an exact title match, owned or shared."""
    for path in ("/albums", "/sharedAlbums"):
        page_token = None
        key = "albums" if path == "/albums" else "sharedAlbums"
        while True:
            params = {"pageSize": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            data = api_get(creds, path, params=params)
            for album in data.get(key, []):
                if album.get("title") == title:
                    return album
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return None


def create_album(creds, title):
    data = api_post(creds, "/albums", {"album": {"title": title}})
    return data


def resolve_album(creds, title, create_if_missing):
    album = find_album(creds, title)
    if album:
        return album
    if create_if_missing:
        print(f"Album '{title}' not found, creating it.")
        return create_album(creds, title)
    sys.exit(f"Album '{title}' not found. Pass --create to create it (upload/sync only).")


# -------------------------------------------------------- media items ----

def list_album_media_items(creds, album_id):
    items = []
    page_token = None
    while True:
        body = {"albumId": album_id, "pageSize": PAGE_SIZE}
        if page_token:
            body["pageToken"] = page_token
        data = api_post(creds, "/mediaItems:search", body)
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


# -------------------------------------------------------------- manifest -

def load_manifest(local_dir, album_id):
    path = os.path.join(local_dir, MANIFEST_NAME)
    if os.path.exists(path):
        with open(path) as f:
            manifest = json.load(f)
        if manifest.get("album_id") == album_id:
            return manifest
    return {"album_id": album_id, "downloaded": {}, "uploaded": {}}


def save_manifest(local_dir, manifest):
    path = os.path.join(local_dir, MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def unique_local_name(local_dir, filename, media_id, manifest):
    """Avoid clobbering files when Google Photos has duplicate filenames
    within the same album."""
    candidate = filename
    existing_id = manifest["downloaded"].get(candidate, {}).get("id")
    if existing_id is None or existing_id == media_id:
        return candidate
    stem, ext = os.path.splitext(filename)
    candidate = f"{stem}_{media_id[:8]}{ext}"
    return candidate


# ------------------------------------------------------------- download --

def download_media_item(creds, item, dest_path):
    base_url = item["baseUrl"]
    is_video = item.get("mediaMetadata", {}).get("video") is not None
    suffix = "=dv" if is_video else "=d"
    resp = requests.get(base_url + suffix, headers=auth_headers(creds), stream=True)
    _raise_for_status(resp)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    with open(tmp_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    os.replace(tmp_path, dest_path)


def do_download(creds, album, local_dir, dry_run=False):
    manifest = load_manifest(local_dir, album["id"])
    items = list_album_media_items(creds, album["id"])
    print(f"Found {len(items)} item(s) in album '{album['title']}'.")

    new_count = 0
    for item in items:
        media_id = item["id"]
        already = any(
            v.get("id") == media_id for v in manifest["downloaded"].values()
        )
        if already:
            continue

        filename = unique_local_name(local_dir, item["filename"], media_id, manifest)
        dest_path = Path(local_dir) / filename

        if dest_path.exists() and filename not in manifest["downloaded"]:
            # A same-named file exists locally but isn't tracked as this
            # item; treat it as already-present local content, don't
            # overwrite it, just record it so we don't retry every run.
            print(f"  skip (local file already exists, untracked): {filename}")
            manifest["downloaded"][filename] = {"id": media_id, "size": dest_path.stat().st_size}
            continue

        new_count += 1
        if dry_run:
            print(f"  would download: {filename}")
            continue

        print(f"  downloading: {filename}")
        download_media_item(creds, item, dest_path)
        manifest["downloaded"][filename] = {
            "id": media_id,
            "size": dest_path.stat().st_size,
        }
        save_manifest(local_dir, manifest)

    if not dry_run:
        save_manifest(local_dir, manifest)
    print(f"Download complete: {new_count} new file(s).")


# --------------------------------------------------------------- upload --

def upload_bytes(creds, file_path):
    mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    headers = auth_headers(creds)
    headers.update(
        {
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol": "raw",
        }
    )
    with open(file_path, "rb") as f:
        resp = requests.post(UPLOAD_URL, headers=headers, data=f)
    _raise_for_status(resp)
    return resp.text  # upload token


def batch_create(creds, album_id, upload_token, filename):
    body = {
        "albumId": album_id,
        "newMediaItems": [
            {
                "simpleMediaItem": {
                    "uploadToken": upload_token,
                    "fileName": filename,
                }
            }
        ],
    }
    return api_post(creds, "/mediaItems:batchCreate", body)


SUPPORTED_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tiff",
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mpg", ".mpeg", ".wmv",
}


def do_upload(creds, album, local_dir, dry_run=False, force=False):
    manifest = load_manifest(local_dir, album["id"])

    local_files = sorted(
        p for p in Path(local_dir).iterdir()
        if p.is_file() and p.name != MANIFEST_NAME and p.suffix.lower() in SUPPORTED_EXT
    )

    new_count = 0
    for path in local_files:
        stat = path.stat()
        record = manifest["uploaded"].get(path.name)
        unchanged = (
            record is not None
            and record.get("size") == stat.st_size
            and record.get("mtime") == stat.st_mtime
        )
        if unchanged and not force:
            continue

        new_count += 1
        if dry_run:
            print(f"  would upload: {path.name}")
            continue

        print(f"  uploading: {path.name}")
        upload_token = upload_bytes(creds, path)
        result = batch_create(creds, album["id"], upload_token, path.name)

        status = result["newMediaItemResults"][0]["status"]
        if status.get("code") not in (None, 0):
            print(f"    failed: {status.get('message')}")
            continue

        media_id = result["newMediaItemResults"][0]["mediaItem"]["id"]
        manifest["uploaded"][path.name] = {
            "id": media_id,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        # Also mark it downloaded so a later `sync` doesn't pull it back.
        manifest["downloaded"][path.name] = {"id": media_id, "size": stat.st_size}
        save_manifest(local_dir, manifest)
        time.sleep(0.2)  # gentle pacing

    if not dry_run:
        save_manifest(local_dir, manifest)
    print(f"Upload complete: {new_count} new/changed file(s).")


# ---------------------------------------------------------------- CLI ----

def build_parser():
    p = argparse.ArgumentParser(description="Sync a local folder with a Google Photos album.")
    p.add_argument("--client-secret", default=DEFAULT_CLIENT_SECRET, help="OAuth client secret JSON file")
    p.add_argument("--token", default=DEFAULT_TOKEN_FILE, help="Path to store/read the OAuth token")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="Run the OAuth flow and store a token")

    def add_common(sp):
        sp.add_argument("--album", required=True, help="Album title")
        sp.add_argument("--dir", required=True, help="Local directory to sync")
        sp.add_argument("--create", action="store_true", help="Create the album if it doesn't exist")
        sp.add_argument("--dry-run", action="store_true", help="Show what would happen without doing it")

    d = sub.add_parser("download", help="Pull remote-only album items into the local folder")
    add_common(d)

    u = sub.add_parser("upload", help="Push local-only files into the album")
    add_common(u)
    u.add_argument("--force", action="store_true", help="Re-upload files even if already tracked")

    s = sub.add_parser("sync", help="Download then upload (two-way, additive only)")
    add_common(s)
    s.add_argument("--force", action="store_true", help="Re-upload files even if already tracked")

    return p


def main():
    args = build_parser().parse_args()

    if args.command == "auth":
        load_credentials(args.client_secret, args.token)
        print("Authenticated. Token saved to", args.token)
        return

    creds = load_credentials(args.client_secret, args.token)
    os.makedirs(args.dir, exist_ok=True)

    create = args.command in ("upload", "sync") and args.create
    album = resolve_album(creds, args.album, create)

    if args.command == "download":
        do_download(creds, album, args.dir, dry_run=args.dry_run)
    elif args.command == "upload":
        do_upload(creds, album, args.dir, dry_run=args.dry_run, force=args.force)
    elif args.command == "sync":
        do_download(creds, album, args.dir, dry_run=args.dry_run)
        do_upload(creds, album, args.dir, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
