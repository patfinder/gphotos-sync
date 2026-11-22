# TODO

## Feature / improvement ideas (from ChatGPT via gpt-connector, 2026-11-21)

Not yet reviewed or prioritised. Notes in *italics* are mine (Claude's), on how each fits what's already built.

1. Content Fingerprinting & Duplicate Detection — Detect duplicates using SHA-256 plus perceptual hashes (pHash) to catch visually identical photos with different encodings or resolutions. Avoid redundant uploads and identify existing duplicates across both accounts. Effort: M

2. Automated Integrity Auditing — Periodically re-hash the entire local archive and verify that every archived file has a corresponding confirmed backup record. Report missing, corrupted, unexpectedly modified, or orphaned files without automatically deleting anything. Effort: M

3. Google Photos UI Health Monitor — Add preflight checks for expected DOM elements, timeline structure, download buttons, and album information panels. Detect Google UI changes early and automatically disable destructive operations when selectors or page behavior become unreliable. Effort: M

4. Deletion Quarantine & Recovery Window — Replace immediate Trash operations with a configurable quarantine period, maintaining a manifest of candidates and deletion decisions. Require a second successful backup verification before moving photos to Trash, with an easy restore workflow. Effort: M

5. SQLite State Database — Migrate operational state from JSONL into SQLite while retaining the original append-only event history. Gain indexed lookups, transactional updates, efficient reconciliation, and easier reporting without sacrificing auditability. Effort: L

6. Backup Reconciliation & Self-Healing — Periodically compare local archive records against actual files and BACKUP account uploads. Automatically retry missing uploads, repair incomplete state records, and flag inconsistencies for investigation. Effort: L

7. Bandwidth-Aware Scheduling — Add configurable download/upload limits, concurrency controls, and adaptive retry delays for rate limits and transient network failures. Prioritize downloading and protecting originals before performing secondary operations. Effort: M

8. Photo Metadata & Timeline Index — Extract EXIF metadata, capture timestamps, GPS coordinates, camera information, and media dimensions into a searchable local index. Provide queries by date, location, camera, and media type without depending on Google Photos UI searches. Effort: M

9. Offline-First Web Dashboard — Build a lightweight local dashboard showing archive size, downloaded/uploaded/deleted counts, last successful run, pending work, errors, and storage growth. Include searchable operation history and safe controls for pausing or resuming processing. Effort: M

10. Disaster Recovery & Restore Verification — Add a recovery command that reconstructs the archive from available local files and backup manifests, verifies hashes, and produces a machine-readable recovery report. Periodically test a sample of BACKUP downloads against original SHA-256 hashes to validate that the backup is genuinely recoverable. Effort: L

*Notes:*
- *#3 and #6 partly exist: the album-check self-test turns deletions off when the page doesn't behave as expected, and every run retries unfinished uploads and deletions from the state file.*
- *#9: the run summary already goes to Loki with its counts, so a Grafana dashboard may cover most of this without building a web UI.*
- *#10: files read back from the backup account may not be byte-identical to the originals (Google can rewrite metadata), so a restore check might need to compare pixels or EXIF rather than SHA-256.*

## v2: move photos by sharing instead of download + upload

**Status:** built as `gphotos_backup_v2.py` (2026-11-21), shared album run by the tool, computer copy kept. Still to do: run `probe` on the real site and adjust labels; first real runs.

Requested 2026-11-21. Share the items from the main account to the backup account. On the backup account, put the shared items in an album. On the main account, delete the shared items.

*Open questions to settle first (Claude's notes):*
- *Deleting the original in the main account removes it from anything it was shared through. The backup account must save its own copy first ("Save to library" / add to its own album) before the main copy is deleted. Need to check that the copy it saves survives that deletion.*
- *Google's Partner Sharing automatically shares the main account's photos (all of them, or from a date) with one partner account, and the partner can auto-save them. It might do most of the sharing step without browser automation.*
- *Both accounts would be driven through photos.google.com (the API can't share or save another account's items), so two signed-in Chrome profiles.*
- *Decide whether v2 still keeps a copy on the computer.*
