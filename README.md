# Calibre libraries for Komga and Grimmory

This repository provides three independent tools. The existing Komga implementation remains in the repository root; Grimmory tools live in `calibre2grimmory/` and do not import or change Komga modules.

| Tool | Entry point | Purpose |
| --- | --- | --- |
| calibre2komga | `calibre2komga.py` | Export or virtually expose ebooks in Komga's series hierarchy, including optional audiobook injection into virtual EPUBs. |
| calibre2grimmory | `calibre2grimmory/calibre2grimmory.py` | A read-only FUSE view retaining Calibre's hierarchy, with native audio folders and optional cached JSON sidecars. |
| Grimmory sidecar importer | `calibre2grimmory/grimmory_import_sidecars.py` | Apply sidecars to Grimmory one book/request/transaction at a time, with live progress and resume. |

## Get the scripts

```bash
git clone https://github.com/eserero/calibre2komga.git
cd calibre2komga
```

For an existing checkout, update it with `git pull --ff-only` when your local changes allow it. All commands below start in the repository root. Replace `/path/to/calibre` (the directory containing `metadata.db`), `/path/to/audiobooks`, and the Grimmory URL with your own locations. The two mount tools use separate mountpoints and can run independently.

If earlier test versions were copied directly into the repository root, switch to the `calibre2grimmory/...` entry points shown below. Git updates the committed subfolder files, not those manually copied root-level scripts.

## 1. calibre2komga: existing Komga workflow

Keep using your existing working Python environment and mount command. Fresh installations can use the Python/FUSE setup in the existing installation section below and the [Komga FUSE guide](docs/USAGE_FUSE.md). Komga's mount uses the pip `fusepy` package's `fuse` module; the apt package's `fusepy` module alone is not a substitute for this unchanged implementation.

For Docker access, enable `user_allow_other` in `/etc/fuse.conf` before using `--allow-other`. Use an empty, unmounted target, outside the sources:

```bash
mkdir -p "$HOME/calibre2komga/komga_virtual"
nohup python3 calibre2komga.py mount \
  /path/to/calibre "$HOME/calibre2komga/komga_virtual" \
  --allow-other \
  --audiobook-column audiobookfiles \
  --audiobook-base-path /path/to/audiobooks \
  --audiobook-exts ".mp3,.m4a,.m4b" \
  > calibre_mount.log 2>&1 &
```

Use the Python executable from your existing environment if it differs from `python3`. To export actual copies instead of mounting:

```bash
python3 calibre2komga.py export /path/to/calibre /path/to/komga-export
```

Komga's optional audio integration injects audio into the virtual EPUB representation. Its code, dependencies, flags, hierarchy and existing behavior are unchanged by the Grimmory additions. Full original Komga documentation follows the Grimmory quick starts below.

## 2. calibre2grimmory: native audio and cached metadata

Requires Python 3.9+; mounting requires Linux and FUSE. On Ubuntu/Debian, system Python can use:

```bash
sudo apt update
sudo apt install python3-fusepy
python3 -c "import fusepy; print('FUSE ready')"
```

The Grimmory mount supports both `fuse` (pip) and `fusepy` (apt) module names. A venv is optional; [the Grimmory guide](calibre2grimmory/README.md) covers that alternative and system libfuse requirements. Enable `user_allow_other` in `/etc/fuse.conf` for `--allow-other`.

Calibre's `audiobookfiles` custom text column contains relative audio-folder paths under the audio root. Multiple lines create separate full virtual book folders, each exposing the original ebook files plus one `audiobook/` folder. Nested audio stays nested. Files are referenced, not copied or embedded in EPUBs.

Preview the view, including optional metadata caching:

```bash
python3 calibre2grimmory/calibre2grimmory.py preview \
  --calibre /path/to/calibre \
  --audio-base /path/to/audiobooks \
  --sidecar-cache "$HOME/.cache/calibre2grimmory" \
  --output grimmory-preview.json
```

Mount it:

```bash
mkdir -p "$HOME/calibre2komga/grimmory_virtual"
nohup python3 calibre2grimmory/calibre2grimmory.py mount \
  --calibre /path/to/calibre \
  --audio-base /path/to/audiobooks \
  --mountpoint "$HOME/calibre2komga/grimmory_virtual" \
  --allow-other \
  --audiobook-column audiobookfiles \
  --sidecar-cache "$HOME/.cache/calibre2grimmory" \
  > grimmory_mount.log 2>&1 &
```

Without `--sidecar-cache`, this is the original passthrough view and no OPF conversion occurs. With it, real generated JSON files are stored only in the dedicated cache directory; virtual `.metadata.json` and `.cover.jpg` files appear beside the media. Calibre's database, OPFs and media stay read-only. Unchanged OPFs reuse cached JSON; changed/new OPFs are converted. Warm starts check file attributes and the cache index, while JSON reads and virtual alias discovery happen on demand. The logs report reused/converted counts, payload size, folder-planning time, cache time, and mount readiness.

Converted fields include title, authors, publisher, publication date, description, language, validated ISBNs, supported provider IDs, primary series/index, tags, category, pages and narrators. `#series2` and `#readinglist` are preserved as prefixed tags. Reading status/progress and other unsupported custom fields are excluded. See the [field mapping and cache details](calibre2grimmory/README.md#virtual-metadata-sidecars).

### Docker, remounts, and metadata import

1. Mount the FUSE view **before** starting Grimmory. Bind-mount the host view read-only, for example `/home/your-user/calibre2komga/grimmory_virtual:/calibre:ro`.
2. Select **One book per folder** and **Prefer Sidecar** in the Grimmory library. Disable metadata/sidecar writing and automatic file moving for this read-only view.
3. After a host FUSE remount, restart the Grimmory container so its bind mount sees the new view. Host visibility alone does not prove container visibility. For example, after replacing the mount, `docker restart grimmory` restarts a container named `grimmory`.
4. Let Grimmory finish its library scan, then run the sequential sidecar importer below to apply Calibre's JSON metadata.

Grimmory 3.5.0's initial format importers read embedded metadata without applying the JSON sidecar preference; this was also checked at develop commit `9a33c5b1ba8c6542b060d265c824b4281c62981b`. EPUBs may look correct because they already contain embedded metadata. Generating JSON alone does not guarantee that a library scan imports it. This is why the separate import step is needed.

The UI also exposes **Settings → Metadata 2 → Library-Specific Overrides → Books → Import Sidecar**. In 3.5.0 that library-wide operation uses one transaction across all books; large libraries can take a long time with no committed progress visible until completion. Leaving the settings page resets its local spinner and unsubscribes the browser's request, so the spinner is not reliable evidence of server completion. The separate script below uses the working single-book endpoint instead.

To inspect or stop either mount, use its own log/target:

```bash
tail -f grimmory_mount.log
fusermount -u "$HOME/calibre2komga/grimmory_virtual"
# Separately, for Komga:
fusermount -u "$HOME/calibre2komga/komga_virtual"
```

Stop consumer activity before unmounting; the target must be empty and unmounted before mounting again. Restart the view after Calibre has saved OPF/path/association changes, then refresh Grimmory and import sidecars. Some nested audiobook playback/grouping behavior remains a Grimmory limitation.

## 3. Grimmory sidecar fix: sequential import with live progress

Requires only Python 3.9+ standard libraries: no FUSE dependency, pip install, or venv is needed for this helper. **Finish or stop any existing library-wide import before running it.** Restarting Grimmory cancels an active bulk request and rolls back its uncommitted transaction; let the server become healthy before beginning a new import.

From the repository root, run against your Grimmory server:

```bash
python3 -u calibre2grimmory/grimmory_import_sidecars.py \
  --url "http://127.0.0.1:6060" \
  --username admin \
  --library Books \
  --state "$HOME/.local/state/calibre2grimmory/books-import.sqlite3"
```

The password is requested interactively and is not saved. Live terminal output shows current book, processed/total, percentage, imported/resumed/unchanged/missing/failed counts, elapsed time and ETA. Each successful book request commits independently and is immediately recorded in the local SQLite checkpoint.

- **Try a small batch:** add `--limit 10`, then remove it to continue through the library.
- **Stop/resume:** Ctrl+C stops the helper; rerun the same command and retain the state file to skip completed books. An interrupted in-flight request may need server verification before resuming.
- **Future metadata updates:** add `--recheck`; completed books are imported again only if their sidecar metadata changed. A normal resume skips completed IDs without rechecking them.
- **Preview:** add `--dry-run` to inspect sidecars without importing or marking books completed.
- **One book:** add `--book-id 81670`, replacing the example ID with a record in the selected library.
- **Load control:** `--delay 0.1` is the default pause between books; `--timeout 120` limits each request. Uncertain POST results stop the run without an automatic retry.
- **Keep a log:** append `2>&1 | tee grimmory_sidecar_import.log`. Piped/redirected progress is printed every five seconds by default; `--progress-seconds 1` changes that interval.

Use a new state file if the library is deleted/recreated or you switch servers. Field locks remain effective; only supplied metadata is applied. The helper does not import cover images or personal reading progress. Full behavior, exit codes and recovery instructions: [IMPORT-SIDECARS.md](calibre2grimmory/IMPORT-SIDECARS.md).

The mount/cache and sequential importer have been confirmed working by the user. Automated validation includes 19 mount/metadata-cache tests and 10 importer tests, including a local HTTP fixture. Run them with:

```bash
python3 calibre2grimmory/test_calibre2grimmory.py
python3 calibre2grimmory/test_grimmory_import_sidecars.py
```

## Existing Komga documentation

The following sections document the original Komga workflow and remain applicable to that tool.

### 🚀 calibre2komga (eserero fork) - Virtual Filesystem Support

This fork of `calibre2komga` adds a powerful **Virtual Filesystem (FUSE)** feature. Instead of copying thousands of files and wasting disk space, this tool can "mount" your Calibre library into a format that Komga understands perfectly.

### 🌟 Key Enhancements in this Fork:
- **Zero-Copy Mirroring**: Use FUSE to expose your Calibre library to Komga without duplicating a single byte.
- **Modular Architecture**: Refactored for better maintenance and separate components for database parsing, exporting, and mounting.
- **Docker Ready**: Includes specific support for `--allow-other` and clear instructions for Docker/Ubuntu environments.

**[👉 Read the FUSE Setup & Mount Guide (USAGE_FUSE.md)](./docs/USAGE_FUSE.md)**

---

## 🛠️ Installation & Setup

### 1. Prerequisites (Linux)
The FUSE feature requires `libfuse2`. On modern Ubuntu versions (22.04+), install it with:
```bash
sudo apt update
# Ubuntu 22.04:
sudo apt install libfuse2
# Ubuntu 24.04:
sudo apt install libfuse2t64
```

### 2. Python Environment
It is recommended to use a virtual environment:
```bash
# Create venv
python3 -m venv venv
# Activate venv
source venv/bin/activate
# Install dependencies
pip install -r requirements.txt
```


# calibre2komga, a Calibre to Komga Migration Script

While I like Calibre (and still plan to use it for ingesting books), I grew dissatisfied with Calibre-Web (and Calibre-Web-Automated).  I liked Komga and wanted to switch, but didn't want to spend time painfully migrating my library to the structure Komga likes.  So I asked my buddy Claude(.ai) to help me write a migration script.  After some back and forth and tweaks, this is the result.  So yes, it's 100% AI coded, but 1) it's non-desctructive to your Calibre library and 2) it's at least been tested by me a bit.  That being said, if you see something/run into issues, by all means raise an issue and we'll work on it.

A Python script that migrates ebooks from [Calibre's](https://github.com/kovidgoyal/calibre) folder structure to [Komga's](https://github.com/gotson/komga) expected format, making it easy to transition your ebook library to Komga's comic/ebook server.

## Important Notes

- **Backup First**: Always backup your libraries before migration
- **Non-destructive**: Original Calibre library remains unchanged
- **File Conflicts**: Existing files in destination are skipped with warnings
- **Series Only**: Books without `.epub` or `.kepub` formats are skipped (Komga only reads epub files so no need to migrate the rest)
- **Metadata**: Only ebook files are copied; metadata and cover files are excluded

## Overview

This script reads Calibre's metadata database to accurately organize books by series and converts the folder structure from Calibre's `Author/Title/files` format to Komga's flat `Series/files` format.

### Before (Calibre Structure)
```
📁 Calibre Library/
├── 📁 Brandon Sanderson/
│   ├── 📁 The Way of Kings (45)/
│   │   └── 📄 The Way of Kings.epub
│   ├── 📁 Words of Radiance (22)/
│   │   └── 📄 Words of Radiance.epub
│   └── 📁 Warbreaker (178)/
│       └── 📄 Warbreaker.epub
```

### After (Komga Structure)
```
📁 Komga Library/
├── 📁 Brandon Sanderson - The Stormlight Archive/
│   ├── 📄 Volume 01 - The Way of Kings.epub
│   └── 📄 Volume 02 - Words of Radiance.epub
└── 📁 Brandon Sanderson/
    └── 📄 Warbreaker.epub
```

## Features

- ✅ **Database-driven**: Uses Calibre's SQLite database for accurate series detection
- ✅ **Series organization**: Groups books properly using Calibre's series metadata
- ✅ **Volume numbering**: Maintains correct order using series index from Calibre
- ✅ **Title cleaning**: Removes Calibre's auto-generated numbering suffixes (e.g., "(84)")
- ✅ **Format filtering**: Only migrates `.epub` and `.kepub` files
- ✅ **Dry run mode**: Preview changes before migration
- ✅ **Author filtering**: Migrate specific authors only
- ✅ **Cross-platform**: Works on Windows, macOS, and Linux
- ✅ **Safe migration**: Preserves original files, copies to new location
- ✅ **Virtual Filesystem (NEW)**: Mount your library as a virtual drive using FUSE (Linux only, zero disk space used!)

## Requirements

- Python 3.6 or higher
- Access to a Calibre library folder
- Destination folder for Komga library
- `fusepy` (for the optional FUSE mount feature)

## Installation

1. Download the script files (`calibre2komga.py`, `calibre_core.py`, etc.)
2. Ensure Python 3.6+ is installed on your system
3. For the FUSE mount feature, install dependencies: `pip install fusepy`

## Usage

### Option A: Virtual FUSE Mount (Zero Disk Space) - Recommended for Linux
This "tricks" Komga into thinking the files are in its preferred structure without actually copying them.

```bash
# Basic mount
python3 calibre2komga.py mount /path/to/calibre /mnt/komga_virtual

# Mount with Audiobook Injection (New!)
python3 calibre2komga.py mount /path/to/calibre /mnt/komga_virtual \
  --audiobook-column "audiobook_path" \
  --audiobook-base-path "/path/to/audiobooks"

# For Docker support (requires editing /etc/fuse.conf)
python3 calibre2komga.py mount /path/to/calibre /mnt/komga_virtual --allow-other
```
See [USAGE_FUSE.md](./docs/USAGE_FUSE.md) for detailed instructions on backgrounding and permissions.

### Option B: Basic Migration (Copying Files)
```bash
python3 calibre2komga.py export /path/to/calibre /path/to/komga
```

---

## 🎧 Audiobook Integration

The virtual filesystem can dynamically merge audiobook folders into your EPUB files on the fly. This allows media servers like Komga to "see" audiobook files (like `.mp3` or `.m4a`) as if they were stored inside the EPUB archive in a subfolder named `audiobook/`.

### How to set it up:
1.  **Calibre Side:** Create a custom column in Calibre (type: "Text") to store the **relative path** to the book's audiobook folder.
2.  **Filesystem Side:** Ensure your audiobooks are stored in a consistent root directory.
3.  **Mount Command:** Use the flags below to tell the script how to link them.

**What happens if flags are missing?**
- If `--audiobook-column` is not provided, the script behaves normally (no audio injection).
- If `--audiobook-exts` is omitted, it defaults to `.mp3,.m4a`.
- If a book has no value in the specified custom column, it is served as a regular EPUB.

---

## Command Line Options

| Option | Description |
|--------|-------------|
| `calibre_path` | Path to your Calibre library directory (required) |
| `komga_path` | Path to your Komga library directory (required, will be created if doesn't exist) |
| `--dry-run` | Show what would be migrated without copying files |
| `--author "Name"` | Filter migration to specific author (case insensitive partial match) |
| `--verbose` | Enable detailed logging output |
| `--allow-other` | Allow other users to access the mount (required for Docker) |
| `--audiobook-column` | Name of the Calibre custom column containing the audiobook relative path |
| `--audiobook-base-path`| Absolute path to the root folder where audiobooks are stored |
| `--audiobook-exts` | Comma-separated list of allowed audio extensions (default: `.mp3,.m4a`) |

## How It Works

1. **Reads Calibre Database**: Connects to `metadata.db` to extract book metadata, series information, and series indices
2. **Series Detection**: Uses Calibre's series metadata for accurate grouping, falls back to title pattern matching for standalone books
3. **Folder Structure**: Creates series folders and places files directly inside (no subfolders)
4. **File Naming**: Renames files to include volume information for series books
5. **Format Filtering**: Only processes `.epub` and `.kepub` files

## File Organization Logic

### For Books in a Series
- **Folder**: `Author - Series Name`
- **Files**: `Volume XX - Book Title.epub`

Example: `Brandon Sanderson - Mistborn/Volume 01 - The Final Empire.epub`

### For Standalone Books
- **Folder**: `Author Name`
- **Files**: `Book Title.epub`

Example: `Brandon Sanderson/Warbreaker.epub`

## Migration Statistics

The script provides detailed statistics after completion:
```
Migration Summary:
  Total books found: 1,247
  Books migrated: 1,198
  Books skipped: 45
  Errors: 4
  Success rate: 96.1%
```

## Troubleshooting

### "No metadata.db found" Error
- Ensure you're pointing to the root Calibre library folder
- The folder should contain a `metadata.db` file

### "No supported ebook files found" Warning
- Book only has formats other than `.epub` or `.kepub`
- Consider converting books in Calibre first if needed

### File Permission Errors
- Ensure you have read access to Calibre library
- Ensure you have write access to destination folder

## Related Projects

- [Calibre](https://github.com/kovidgoyal/calibre) - E-book management software
- [Komga](https://github.com/gotson/komga) - Media server for comics/ebooks


---

## 📦 Virtual ZIP Utility (`virtual_zip.py`)

A standalone utility that virtually merges an existing ZIP file with an external directory. It presents them as a single valid ZIP file via FUSE. This is used by the main script to inject audiobooks into EPUB files on the fly.

### Usage:
```bash
python3 virtual_zip.py /path/to/base.zip /path/to/extra_files /path/to/mount_dir --target-dir "audiobook" --exts ".mp3,.m4a"
```

### 🧪 Testing the Utility
We have included a test data setup to verify the engine works:

1. **Create Test Data:**
   ```bash
   mkdir -p test_data/extra
   python3 -c "import zipfile; z = zipfile.ZipFile('test_data/base.zip', 'w'); z.writestr('original.txt', 'Original content'); z.close()"
   echo "Audio 1" > test_data/extra/audio1.mp3
   echo "Audio 2" > test_data/extra/audio2.m4a
   mkdir -p test_mount
   ```

2. **Run the Virtual Mount:**
   ```bash
   source venv/bin/activate
   python3 virtual_zip.py test_data/base.zip test_data/extra test_mount
   ```

3. **Verify (In another terminal):**
   ```bash
   # List the virtual ZIP content
   unzip -l test_mount/base.zip
   # Verify integrity (CRC check)
   unzip -t test_mount/base.zip
   # Extract to verify data
   mkdir -p extracted_test && unzip test_mount/base.zip -d extracted_test
   ```

---

---

**Disclaimer**: This script is not officially affiliated with Calibre or Komga projects. Use at your own risk and always backup your data before migration.
