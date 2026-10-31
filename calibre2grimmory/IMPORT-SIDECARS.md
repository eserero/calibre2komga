# Import sidecars one book at a time

`grimmory_import_sidecars.py` is a separate Python 3.9+ script using only the standard library. No pip packages or venv are needed. It does not change the FUSE mount script or any Komga files.

The importer calls Grimmory's working single-book endpoint sequentially. Each successful request commits its own server transaction, and then the script commits that success to a small local SQLite resume database. It never calls the library-wide `import-all` endpoint.

## Before running

Let any existing library-wide sidecar import finish, or stop it before using this script. Restarting the Grimmory container cancels an active request and rolls back its uncommitted bulk transaction; allow Grimmory to become healthy again before starting this importer. The script cannot detect an existing server-side bulk import, so do not run both together.

Make sure the FUSE mount is active and sidecars are visible inside Grimmory's container. A host remount may require restarting the container so it sees the new mount. Finish library scanning before starting the importer.

## Run interactively

From the repository root, run the committed helper:

```bash
cd "$HOME/calibre2komga"
python3 -u calibre2grimmory/grimmory_import_sidecars.py \
  --url "http://10.0.0.110:6060" \
  --username admin \
  --library Books \
  --state "$HOME/.local/state/calibre2grimmory/books-import.sqlite3"
```

It prompts for the Grimmory password without displaying or saving it. The server/library identity is printed before processing. The book list is fetched once, scoped to the exact named library; no other library is imported.

Replace the server URL if needed. If you previously copied the helper directly into the repository root, either use the committed path above or manually replace that copy; a git pull updates the subfolder version.

The terminal updates for each book, showing current/total, percentage, imported, resumed, unchanged, planned (dry-run), missing, failed, elapsed time, ETA and current book title/ID. Long network requests show which book is being checked/imported; the request timeout defaults to 120 seconds. ETA is an estimate and can fluctuate as server response times change.

Example progress (illustrative):

```text
[125/19000   0.7%] imported=120 resumed=0 unchanged=0 planned=0 missing=5 failed=0 elapsed=00:02:10 ETA=05:27:10 | Processed #81670 The Lorax
```

Test a few pending books first by adding `--limit 10`. Remove that option for the full run; completed books are remembered. To target one specific record, add `--book-id 81670` (repeatable). IDs must belong to the selected library.

## Stop and resume

Press Ctrl+C to stop. Run the identical command again to resume. Successful books are skipped immediately, without fetching their sidecars again. The default checkpoint file is `grimmory-sidecar-import.sqlite3` in the working directory; retain it. Set `--state /path/to/progress.sqlite3` to choose a stable location.

If interruption occurs after the server commits a book but before its checkpoint is written, that one book may be imported again on resume. A timeout, broken connection or HTTP 5xx during an import stops further writes because the result may be uncertain; check whether the server is still processing the book before resuming. The script does not automatically retry an uncertain import.

Each state file is tied to its server URL and numeric library ID. If you delete/recreate the library, use a new state file. An OS lock prevents two importer instances from using the same checkpoint simultaneously, and releases automatically after a crash. The `.lock` file can remain.

## After future Calibre metadata changes

Once the JSON cache/view is updated and Grimmory can read the new sidecars, add:

```bash
--recheck
```

This reads sidecars for previously completed books, compares a fingerprint of their metadata, and imports only changed or new metadata. Generation timestamps are excluded from the fingerprint. A normal resume deliberately skips completed IDs; use `--recheck` for future metadata updates.

### Optional fast change detection from the local JSON cache

When the helper runs on the machine with the mount's JSON cache, add this option while retaining your existing `--state` file:

```bash
--sidecar-cache "$HOME/.cache/calibre2grimmory"
```

This automatically enables incremental change checks using the converter's `index.json`. It skips unchanged successful entries without fetching their sidecars through the API. For changed/new entries it fetches Grimmory's sidecar and checks that its metadata agrees with the local cached JSON before importing. If the cache and running mount disagree, the entry is reported as failed rather than checkpointing stale metadata: refresh/remount and restart the container as appropriate, then resume.

The first run enabling this option for an older checkpoint verifies each completed entry once to establish its local-cache association. Subsequent unchanged runs skip those per-book calls. Ambiguous/unmatched cache paths fall back to API checks. The library book list is still fetched once each run; fast mode does not make startup instantaneous and only notices changes already recorded by the mount's converter cache. Update the mount/cache after Calibre changes before invoking the importer.

Without a local cache, the original `--recheck` behavior remains available. To force full sidecar checks instead of cache-index hints, omit `--sidecar-cache` and use `--recheck`. No FUSE mount behavior was changed by this option.

## Other controls and logs

- `--dry-run`: inspect sidecars and show what would be imported without changing Grimmory or marking books completed. It may create the local checkpoint database/lock file.
- `--delay 0.1`: default pause between books, adjustable to reduce server load.
- `--timeout 120`: per-request timeout; the initial library list may need a larger value on a busy server.
- `--max-errors 5`: stop after this many failed books. Missing sidecars are counted separately and are not checkpointed, so later runs check them again.
- `--progress-seconds 5`: output interval when stdout is redirected or piped. Interactive terminal output updates per book. All output is flushed immediately.

To keep a log while watching progress:

```bash
python3 -u calibre2grimmory/grimmory_import_sidecars.py \
  --url "http://10.0.0.110:6060" --username admin --library Books \
  --state "$HOME/.local/state/calibre2grimmory/books-import.sqlite3" \
  2>&1 | tee grimmory_sidecar_import.log
```

For noninteractive execution, supply the password using the `GRIMMORY_PASSWORD` environment variable through your preferred secret-handling mechanism; credentials/tokens are never written to the checkpoint or progress log. Otherwise the interactive password prompt is required.

The helper uses Grimmory's existing sidecar-import behavior: only provided metadata fields replace stored values, field locks remain effective, and personal reading progress is not imported. A successful count means Grimmory accepted the individual import request; it does not independently verify every field afterward. It does not import cover images; Grimmory's explicit sidecar import treats covers separately. Keep file-writing/auto-moving features disabled for the read-only FUSE library.

Exit codes: `0` completed (possibly with missing sidecars), `2` completed with per-book failures, `1` stopped due to an error, `130` interrupted. Completed checkpoints remain available after any stop.

## Validation

```bash
python3 calibre2grimmory/test_grimmory_import_sidecars.py
```

Thirteen tests cover individual HTTP endpoints, authentication renewal, sequential processing, resume, a single changed sidecar, missing/invalid sidecars, library identity protection, dry-run, limits, checkpoint locking, preservation of committed successes after an uncertain request, fast-cache skipping, cache/mount agreement, set ordering and legacy checkpoints. A local HTTP fixture validates the real client without touching a live Grimmory library. The single-book server endpoint was verified against actual records, and the user subsequently confirmed that the sequential helper works on the live installation; the new fast-cache path is locally tested.
