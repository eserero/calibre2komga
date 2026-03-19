# 🚀 calibre2komga (eserero fork) - Virtual Filesystem Support

This fork of `calibre2komga` adds a powerful **Virtual Filesystem (FUSE)** feature. Instead of copying thousands of files and wasting disk space, this tool can "mount" your Calibre library into a format that Komga understands perfectly.

### 🌟 Key Enhancements in this Fork:
- **Zero-Copy Mirroring**: Use FUSE to expose your Calibre library to Komga without duplicating a single byte.
- **Modular Architecture**: Refactored for better maintenance and separate components for database parsing, exporting, and mounting.
- **Docker Ready**: Includes specific support for `--allow-other` and clear instructions for Docker/Ubuntu environments.

**[👉 Read the FUSE Setup & Mount Guide (USAGE_FUSE.md)](./USAGE_FUSE.md)**

---

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

# For Docker support (requires editing /etc/fuse.conf)
python3 calibre2komga.py mount /path/to/calibre /mnt/komga_virtual --allow-other
```
See [USAGE_FUSE.md](./USAGE_FUSE.md) for detailed instructions on backgrounding and permissions.

### Option B: Basic Migration (Copying Files)
```bash
python3 calibre2komga.py export /path/to/calibre /path/to/komga
```

### Dry Run (Recommended First for Export)
Preview what will be migrated without making any changes:
```bash
python3 calibre2komga.py export /path/to/calibre /path/to/komga --dry-run
```

### Migrate Specific Author
```bash
python calibre2komga.py /path/to/calibre/library /path/to/komga/library --author "Brandon Sanderson"
```

### Verbose Output
```bash
python calibre2komga.py /path/to/calibre/library /path/to/komga/library --verbose
```

### Combined Options
```bash
python calibre2komga.py /path/to/calibre/library /path/to/komga/library --dry-run --author "Isaac Asimov" --verbose
```

## Command Line Options

| Option | Description |
|--------|-------------|
| `calibre_path` | Path to your Calibre library directory (required) |
| `komga_path` | Path to your Komga library directory (required, will be created if doesn't exist) |
| `--dry-run` | Show what would be migrated without copying files |
| `--author "Name"` | Filter migration to specific author (case insensitive partial match) |
| `--verbose` | Enable detailed logging output |

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

**Disclaimer**: This script is not officially affiliated with Calibre or Komga projects. Use at your own risk and always backup your data before migration.
