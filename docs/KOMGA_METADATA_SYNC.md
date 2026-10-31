# Calibre metadata sync for Komga

`komga_sync_metadata.py` is a separate Python 3.9+ helper. It reads Calibre's database/OPFs and uses Komga's metadata API. Existing mount scripts, EPUB contents, folders and series membership are not modified. Series titles are never included in its API updates.

Run it from a checkout containing the helper plus `calibre_core.py`, `calibre2grimmory/calibre2grimmory.py` and `calibre2grimmory/grimmory_import_sidecars.py`. These modules provide the existing mount's naming rules, OPF parsing and progress/error handling. There are no new pip requirements; the helper does not import FUSE. An update ZIP should be extracted into the repository root to supply the new metadata helper files.

## Preview PDFs

After the Komga mount and library scan are up to date, run from the repository root. Replace the example server address, admin login and Calibre path:

```bash
python3 -u komga_sync_metadata.py \
  --url "http://localhost:25600" \
  --username "you@example.com" \
  --library Books \
  --calibre "/path/to/calibre" \
  --file-types pdf \
  --state "$HOME/.local/state/calibre2komga/metadata-sync.sqlite3" \
  --report komga-metadata-preview.json
```

The password is requested without echoing or saving it. `KOMGA_PASSWORD` can alternatively supply it via the environment. Admin access is required so book paths are visible and metadata can be updated. The preview makes only GET requests; it may populate the local metadata cache/checkpoint database and writes the requested report outside the Calibre source.

Review the proposed changes, then add `--apply` to the same command to send individual metadata PATCH requests. Add `--limit 10` to process at most ten proposed updates first. Both preview and apply modes show live progress. Each successful PATCH is checkpointed immediately for resume.

## File-type filter

```bash
--file-types pdf                       # PDFs only
--file-types pdf,epub                  # PDFs and EPUBs
--file-types .PDF --file-types cbz      # repeatable; case-insensitive; optional leading dot
```

Omit the option for all types. Any single filename extension is accepted; it is not a fixed list. This does not add new format support to Komga or the existing mount: only books already visible in Komga and unambiguously matched to that mount's naming rules can be updated. Multi-part extensions and MIME types are not filter names; use the final suffix, for example `epub`.

The filter controls remote updates, not the source/index discovery phase. Local metadata-cache checks and paginated Komga index reads still cover the library. A PDF-only run skips EPUB book updates. Shared series metadata is updated only if every active book in that series is selected and matched; a mixed PDF/EPUB series is skipped for shared fields during a PDF-only run. Using the same state file later with other file types is supported: completed PDFs are remembered independently of newly selected formats.

## Metadata fields

| Calibre OPF value | Komga field |
| --- | --- |
| Title | Book title |
| Authors | Book authors, role `writer` |
| Comments/synopsis | Book summary; HTML is converted to readable paragraph text |
| Publication date | Book release date |
| Validated ISBN-13, otherwise ISBN-10 | Book ISBN |
| Primary series index | Book number and numeric sort number |
| Tags, plus secondary-series/reading-list tags | Book tags |
| Publisher and language | Series descriptive fields, only when all members supply the same value |
| Custom category | Union of series genres, only when the whole series is selected and matched |

Fields with no usable source value are omitted rather than cleared. In particular, deleting an OPF field does not automatically erase its old Komga value. Supplied author/tag arrays replace the corresponding Komga arrays; unrelated fields, artwork, read progress, collections and read lists are not changed. Secondary series and reading lists are represented as tags, not created as native collections/read lists by this version. Pages, narrators and provider IDs do not have mapped Komga fields here.

Series title and grouping remain derived from your working folder layout. Renaming a series through Komga's metadata API can change its displayed title, but cannot move books between folder-based series; this helper intentionally performs neither operation.

## Matching and incremental behavior

- The helper reads `metadata.db` through a read-only SQLite connection and uses the existing `CalibreMetadataStore` naming methods to predict each virtual path. It matches that complete path against Komga's book URL relative to its library root. It never guesses by title. Unmatched paths and collisions are reported and skipped; zero matches for a nonempty selection is an error.
- Local state stores the target server/library/Calibre root, cached parsed OPFs, directory filenames and fingerprints of successful book/series updates. Unchanged OPFs reuse parsed metadata based on their file attributes. This SQLite file is separate from Calibre's and Komga's databases and must remain outside the Calibre library.
- On normal repeat runs, unchanged successfully synced payloads are skipped. A changed/new source payload is compared to the metadata in Komga's paginated book/series indexes; only differing supplied fields are PATCHed. There are no per-book GET calls and no bulk metadata update request.
- After manual edits, unlocking fields, or a Komga metadata refresh that overwrites unlocked values, add `--force` to recompare checkpointed fields against the current server values. It still PATCHes only differences, not every book indiscriminately.
- Existing field locks are respected by default. `--overwrite-locked` explicitly permits changing values of locked fields; it does not change the locks themselves. Unlocked API metadata may be overwritten by later Komga embedded-metadata refreshes. Locked/skipped fields are reported; after changing locks use `--force` to revisit them.
- Restart the mount and scan Komga after Calibre folder/title/series changes that affect virtual paths. Make sure Calibre has finished saving OPFs before syncing. Otherwise paths may not match or the descriptive metadata may lag behind the database.
- Ctrl+C stops the helper; rerun with the same state file to resume. A timeout/disconnect/HTTP 5xx on a PATCH stops the run because its outcome may be uncertain. Check Komga before resuming; an in-flight book may need rechecking. Every prior successful request remains checkpointed.

Progress lists imported (successful metadata updates), resumed, unchanged, planned, missing and failed counters; some are shared with the Grimmory progress formatter and may remain zero. Matching and consensus warnings are printed separately. Stop on an HTTP error rather than flooding the server; adjust `--timeout`/`--delay` if needed. The state file is protected from concurrent use by an OS lock. A recreated library requires a new state file.

## Validation

```bash
python3 test_komga_sync_metadata.py
```

Thirteen tests cover read-only previews, pagination/library scope, exact and ambiguous matching, locks, series consensus, file-type filters and mixed-series isolation, incremental skipping, a single changed OPF, successful checkpoint preservation after an uncertain update, and a local HTTP fixture with Basic authentication and individual PATCHes. No live Komga updates have been performed by these tests; verify the preview against your installation before applying.
