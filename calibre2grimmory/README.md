# calibre2grimmory

A standalone, read-only FUSE view of a Calibre library with native audiobooks for Grimmory. This directory is independent of calibre2komga: it imports no Komga modules, uses a separate mount point and does not change Komga's configuration or dependencies.

The default is the original passthrough view. Add `--sidecar-cache DIRECTORY` to enable Calibre OPF conversion with persistent JSON caching. Only the dedicated cache directory receives real generated files; Calibre and audiobook sources remain unchanged.

## What it exposes

- Calibre's existing author/book folder structure and original book-directory contents.
- An associated audio folder as `audiobook/`, with its filenames and nested directories unchanged.
- One separate full virtual book folder for each nonempty line in `audiobookfiles`. Each name includes `Audio 01`, `Audio 02`, etc., and a source-folder label. Ebook files are referenced again, not copied.
- Audio-only Calibre records without generating fake EPUBs.

Original files are read directly. Nothing is injected into EPUBs, copied, transcoded, or written to the Calibre database. The database itself is not exposed in the virtual library.

## Linux setup: system Python (Ubuntu/Debian)

Requires Python 3.9+ and FUSE. From this directory:

```bash
sudo apt update
sudo apt install python3-fusepy
python3 -c "import fusepy; print('FUSE ready')"
mkdir -p ~/grimmory_virtual
```

The Debian/Ubuntu package calls its Python module `fusepy`; the pip package calls it `fuse`. The script supports both names. Installing `python3-fuse` is not a substitute for `python3-fusepy`.

To allow Grimmory running as another user or inside Docker to access the mount, edit `/etc/fuse.conf`:

```bash
sudo nano /etc/fuse.conf
```

Ensure this line is present and uncommented:

```text
user_allow_other
```

That enables the `--allow-other` mount option. Without this option the mount is available only to the mounting user (subject to FUSE's normal rules).

## Preview

Replace the source paths with paths visible on the Linux host:

```bash
python3 calibre2grimmory.py preview \
  --calibre /path/to/calibre \
  --audio-base /path/to/audiobooks \
  --sidecar-cache "$HOME/.cache/calibre2grimmory" \
  --output preview.json
```

The preview lists each virtual folder and its original source paths, with warnings and a `sidecar_summary` containing sizes and preparation time. It does not mount anything. With `--sidecar-cache`, preview also creates/updates converted JSON and its index in the dedicated cache directory; the requested report and cache are outside the source folders. Preview also works on Windows without FUSE; pass the actual Windows source paths. Keep reports and the cache private because they include library names, metadata and paths.

For a small trial, add `--book-id 123 --book-id 456` using IDs from your library. The same filter works with `mount`.

## Mount

Use a dedicated empty mount point, separate from Komga and both source directories:

```bash
nohup python3 calibre2grimmory.py mount \
  --calibre /path/to/calibre \
  --audio-base /path/to/audiobooks \
  --mountpoint "$HOME/grimmory_virtual" \
  --audiobook-column audiobookfiles \
  --sidecar-cache "$HOME/.cache/calibre2grimmory" \
  --allow-other \
  > grimmory_mount.log 2>&1 &
```

The process runs in the foreground internally; `nohup` and `&` keep it running in the background. Check startup:

```bash
tail -f grimmory_mount.log
mountpoint "$HOME/grimmory_virtual"
```

To unmount, first stop library activity, then:

```bash
fusermount -u "$HOME/grimmory_virtual"
```

Use `fusermount3` if that is the command installed on your system. An unmounted directory must be empty before mounting. If it is not, inspect it; do not delete its contents blindly. Missing-audio warnings do not prevent startup, but invalid mount points do.

There is no `--audiobook-exts` option: audio folders are exposed unchanged. Grimmory decides which formats it can index/play.

## Grimmory configuration

Mount the FUSE view before starting the Grimmory container. Expose the view using a read-only bind mount, for example (replace the host path):

```yaml
volumes:
  - /home/your-user/grimmory_virtual:/books/calibre:ro
```

Create a small test library with **Book Per Folder** organization. Disable file metadata writing and automatic file moving for this read-only view.

**Mount before starting the Grimmory container. After remounting the host FUSE view, restart Grimmory so the container sees the new mount.** A sidecar readable on the host may remain invisible in a container still referencing the previous mount. This was observed and resolved during live testing.

**Grimmory's scan is not the final metadata-import step.** In Grimmory 3.5.0, initial import reads embedded metadata even with Prefer Sidecar selected; JSON sidecars are readable through a separate explicit import. The same code path was checked at develop commit `9a33c5b1ba8c6542b060d265c824b4281c62981b`. After scanning, use the [sequential sidecar importer](IMPORT-SIDECARS.md) to apply the Calibre metadata. The UI's library-wide Import Sidecar action performs the entire import in one transaction and proved too slow for the user's large library. The helper imports and checkpoints each book separately, with visible progress and resume.

### Virtual metadata sidecars

Enable conversion with `--sidecar-cache DIRECTORY` and choose **Prefer Sidecar** in Grimmory. Existing `metadata.opf` and cover files remain exposed unchanged. The view presents `Book.metadata.json` beside `Book.epub` (and other supported ebook formats), and `Book.cover.jpg` as an alias of Calibre's `cover.jpg`. JSON files are stored in the dedicated cache and loaded on first access for serving. Cover bytes are read from the original file; nothing is copied into the library or embedded in EPUBs.

Use a new or empty dedicated cache directory on a local SSD if available, outside the Calibre source, audiobook source and mountpoint. It contains hashed `*.metadata.json` filenames, `index.json` mapping them to source OPFs, and a marker identifying it as this script's cache. These files contain your book metadata and source paths. Cache files from previous book paths are retained, but are not served for deleted/missing OPFs.

The first cached start converts each OPF once. Later starts compare the source path, nanosecond modification/change times, size, and converter version. Unchanged OPFs are not read or parsed; their cached JSON is reused without rewriting its file or changing its timestamp; individual JSON files are opened and checksum-verified only when requested. Only changed/new OPFs are converted. Missing or corrupt cached JSON is repaired when first requested, and a converter-version change rebuilds affected entries. Cache writes use temporary files plus atomic replacement; source OPFs are always read-only. An unchanged run does not rewrite the cache index either.

Cached mode discovers virtual sidecar and cover filenames only when Grimmory visits a directory. It does not recursively walk the media collection during startup. This defers directory discovery to browsing/scanning; it does not eliminate Grimmory's own scan work. Every startup still reads the Calibre database, builds the folder plan, checks OPF file attributes, and loads the small cache index, so a warm start is faster but not instantaneous. These are startup snapshots, not a live watcher.

Without `--sidecar-cache`, no conversion runs. `--no-sidecars` explicitly requests the same default. `--memory-sidecars` retains the earlier eager in-memory conversion for comparison; these three options are mutually exclusive.

For audio, sidecars are exposed beside MP3/M4A/M4B tracks, including nested tracks, and as `audiobook.metadata.json` beside the `audiobook/` directory for Grimmory's directory-based audiobook lookup. This does not change nested-disc playback limitations. Test discovery and grouping with your installed Grimmory release before a full import.

Converted fields: title, authors, publisher, publication date, HTML description, language, validated ISBN-10/13, supported provider identifiers, series name/index, Calibre tags, `#category`, `#pages`, and `#abs_narrator`. Unknown/placeholder dates (including year 0101) are omitted. Decimal series positions are retained. Selected common language codes are normalized to two-letter codes; others are retained.

`#series2` becomes `Secondary series: <value>` tags and `#readinglist` becomes `Reading list: <value>` tags. These do not create native shelves or a second native series. Reading status, read/unread, progress, personal ratings, word counts and other unsupported custom fields are excluded. Missing values are not invented, and personal ratings are not exported as provider ratings.

The OPF is authoritative for descriptive metadata; `metadata.db` remains authoritative for paths and audiobook associations. Make sure Calibre has saved recent metadata changes to its OPFs before restarting. Missing or malformed OPFs produce warnings and leave original files available without generated sidecars. If a source file already has the generated sidecar/cover name, the generated Calibre version takes precedence only in the virtual view and a warning is logged; the original remains untouched in its source folder. Use `--no-sidecars` to restore the original passthrough behavior.

Preparation is a startup snapshot: cached JSON is shared by all virtual editions and formats for a book. Restart the mount after OPF, cover, path or audiobook changes, then import/refresh sidecar metadata in Grimmory. Unchanged virtual JSON retains its cached modification time across restarts. Updating the virtual files alone does not guarantee that Grimmory refreshes existing records. Disable Grimmory's sidecar **Write on Scan** and **Write on Update**, since this mount is read-only.

### Logging and size reporting

The existing `> grimmory_mount.log 2>&1` command captures timestamped logs without new options. Logs include progress every 500 prepared folders, warnings, and a final preparation summary with:

- Virtual book folder count, reused cache entries, converted OPFs, and skipped OPFs.
- OPF bytes actually read and total JSON payload size indexed. A warm unchanged run should report zero conversions and zero OPF bytes read.
- Folder-planning time, cache preparation time, and total startup time.
- A separate `FUSE mount ready` message after FUSE initializes successfully.

Sizes are reported in MiB. JSON size excludes the index, filesystem overhead, old unused cache entries and Python object overhead; it is not total disk usage or process RAM. Cached mode does not count all virtual aliases at startup because they are discovered on demand (the preview marks `aliases_on_demand: true`). Eager `--memory-sidecars` mode also reports total exposed JSON size. Media and cover sizes are excluded, and no media files are read to calculate totals. Linux timings depend on storage and can differ from Windows preview measurements.

Measured Windows preview for 13,033 OPFs / 13,062 virtual folders: cache population took 117.3 seconds total (72.0 seconds converting); the next unchanged start took 54.8 seconds total, with 4.9 seconds checking the cache, 13,033 hits, zero conversions and zero OPF content bytes read. The remaining approximately 50 seconds was folder planning/filesystem checks. JSON payload size was 17.39 MiB. These measurements exclude Linux FUSE initialization, on-demand JSON reads, and Grimmory's scan/import work; they are not a Linux performance guarantee.

References:
- [Grimmory issue #2466: Calibre OPF series import](https://github.com/grimmory-tools/grimmory/issues/2466)
- [Adjacent OPF import proposal #867 (closed without merge)](https://github.com/grimmory-tools/grimmory/pull/867)

The JSON sidecars address the adjacent-OPF limitation described in those references.

## Optional isolated Python environment

A venv is optional. To avoid changing system Python packages:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import fuse; print('FUSE ready')"
```

Use `.venv/bin/python` instead of `python3` in the commands above. The system libfuse library is still required. Do not reuse or alter Komga's environment.

## Behavior and limitations

- Custom columns are discovered by label; normalized/link-table and direct-value storage are supported.
- `audiobookfiles` is authoritative; `abs_link_url` does not filter valid associations.
- Windows backslashes are normalized on Linux. Absolute paths and paths escaping the configured source roots are rejected.
- Missing audio produces warnings; ebooks remain visible when available. No other audio edition is silently substituted.
- Existing `audiobook` name conflicts and virtual folder collisions stop startup.
- Nested discs/parts remain nested. Grimmory playback of these may be incomplete; flattening is deferred.
- Metadata titles are unchanged. Split folders may display the same title in Grimmory even though their folder labels differ.
- No media conversion, online metadata enrichment, grouping of duplicate Calibre records, or source cleanup is performed.
- Mapping is built at startup; restart the mount after association/path changes, then rescan Grimmory. FUSE may not emit watcher events for changes to the underlying files. Stop consumers before remounting.
- File reads, including seeking, are direct. Database/media writes through the mount are prohibited. Out-of-root symlink targets cannot be opened.

## Tests and validation

For separately committed, sequential sidecar imports with live progress and resume, see [IMPORT-SIDECARS.md](IMPORT-SIDECARS.md). The standalone `grimmory_import_sidecars.py` helper is independent of the FUSE mount process and avoids Grimmory's library-wide transaction.

```bash
python3 test_calibre2grimmory.py
```

Nineteen tests cover multiline entries, original bytes and offset reads, read-only database access, nested audio, audio-only and ebook-only records, rejected writes, path traversal, missing sources, custom-column storage layouts, OPF mapping, virtual sidecar reads/sizes, cover aliases, name conflicts, missing/malformed OPFs, opt-out, cache hits without OPF parsing or rewrites, single-OPF invalidation, corruption/missing-cache repair, version invalidation, cache-directory isolation, on-demand aliases and stable timestamps across restarts.

Windows preview/byte-read tests and Linux user testing confirmed the FUSE mount, cached JSON sidecars, and separate sequential sidecar import workflow. Single-book API imports were verified against real records; the user subsequently confirmed that the helper works. The metadata import and container-remount limitations above remain relevant, and complete playback compatibility for every nested audio layout is not asserted.
