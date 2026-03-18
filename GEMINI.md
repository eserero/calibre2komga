# calibre2komga - Calibre to Komga Migration Script

This project provides a specialized migration script to transition an ebook library from Calibre's nested folder structure to Komga's flat, series-oriented structure.

## Project Overview

*   **Purpose:** Automates the reorganization of ebook files (`.epub`, `.kepub`) from Calibre's `Author/Title/` format to Komga's preferred `Series/` format.
*   **Key Logic:** Uses Calibre's internal SQLite database (`metadata.db`) to accurately group books by series and maintain correct volume numbering.
*   **Safety:** The script is non-destructive; it copies files to the destination rather than moving or deleting them.

## Tech Stack

*   **Language:** Python 3.6+
*   **Database:** SQLite (via standard library `sqlite3`)
*   **Dependencies:** None (uses standard library: `os`, `shutil`, `argparse`, `logging`, `pathlib`, `re`).

## Usage & Operations

### Basic Execution
```bash
python calibre2komga.py /path/to/calibre/library /path/to/komga/library
```

### Common Flags
*   **Dry Run:** `--dry-run` (highly recommended for first-time use to preview changes).
*   **Filter by Author:** `--author "Author Name"` (case-insensitive partial match).
*   **Verbose Logging:** `--verbose` (useful for debugging).

### Testing & Validation
Since this is a utility script, validation is typically done via the `--dry-run` flag. Before committing any significant logic changes, verify:
1.  Path validation still correctly identifies `metadata.db`.
2.  SQL queries in `load_calibre_metadata` handle edge cases (missing authors, series).
3.  Filename sanitization remains compatible across OS boundaries.

## Development Conventions

*   **Standard Library Only:** Avoid adding external dependencies to keep the script portable and easy to run without a virtual environment.
*   **Class-Based Structure:** Most logic is encapsulated within the `CalibreKomgaMigrator` class.
*   **Logging:** Use the standard `logging` module for all output instead of `print` statements.
*   **Metadata First:** Always prefer data from `metadata.db` over parsing file system paths, as Calibre's DB is the source of truth.
*   **Cross-Platform Safety:** Use `pathlib` for all path manipulations and the `sanitize_filename` method for creating new files/folders.

## Key Files
*   `calibre2komga.py`: The main (and only) source file containing the migration logic.
*   `README.md`: Detailed user documentation and examples.
*   `metadata.db` (external): Required at runtime in the source directory.
