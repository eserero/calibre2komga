# Implementation Plan: Generic Virtual ZIP Merger (V4)

## Overview
The core requirement is to expose Calibre `.epub` files (which are ZIP archives) merged with an external folder of audio files, presented as a single file dynamically within the FUSE filesystem.

To ensure stability, reusability, and clean architecture, the virtual merging engine will be built as a **completely generic, standalone utility**. It will be capable of taking *any* existing ZIP file and virtually merging it with the contents of *any* directory (optionally filtered by file extensions). 

This does not add extra complexity; in fact, it simplifies the architecture by cleanly separating the ZIP byte-stitching logic from the Calibre domain logic.

## Phase 1: The Generic Virtual ZIP Engine (Standalone)
**Target File:** `virtual_zip.py` (New File - Generic Utility)

We will implement a standalone module `virtual_zip.py` containing the `VirtualZipMapper` class and a standalone CLI.

**Features:**
1.  **Generic Input:** Takes a base ZIP file path, an external directory path to inject, a virtual target directory name (e.g., `audiobook/`), and a list of allowed extensions (e.g., `.mp3`).
2.  **Zero-Copy Merging:**
    *   Reads the EOCD of the base ZIP to locate its Central Directory.
    *   Scans the external directory for matching files.
    *   Generates standard ZIP Local File Headers (LFH) in memory for the external files.
    *   Generates a new Central Directory in memory combining original entries with the new entries.
    *   Generates a new EOCD in memory.
3.  **Extent Mapping & Read API:** Maps virtual byte ranges to physical sources (base ZIP, external files, or in-memory headers). Exposes `get_virtual_size()` and `read(offset, length)`.

## Phase 2: Standalone FUSE Integration & Testing Utility
**Target File:** `virtual_zip.py` (Extending the standalone utility)

We will add a CLI entry point to `virtual_zip.py` so it can be run completely independently of `calibre2komga.py`.

**Usage:**
`python virtual_zip.py /path/to/base.zip /path/to/extra_files /path/to/mount_dir --target-dir "audiobook" --exts ".mp3,.m4a"`

**Implementation:**
1.  Create `SingleFileFuse` within `virtual_zip.py` to expose the virtualized ZIP file via a temporary FUSE mount.
2.  This allows developers (and users) to test the engine on arbitrary files.

**Testing Criteria:**
1.  Run the generic utility to mount a test ZIP.
2.  Run `unzip -t /path/to/mount_dir/base.zip` to verify the virtual structure is 100% valid.
3.  Extract files to ensure data integrity.

## Phase 3: Calibre DB Integration & Main Codebase Updates
**Target Files:** `calibre_core.py`, `calibre2komga.py`, `virtual_fs.py`

*Only start this phase after Phase 2 is fully tested and verified.*

1.  **CLI Arguments (`calibre2komga.py`):** Add flags to the main `mount` command: `--audiobook-column`, `--audiobook-base-path`, `--audiobook-exts`.
2.  **DB Query (`calibre_core.py`):** 
    *   Update `CalibreMetadataStore.load()` to fetch the audiobook relative path from the specified Calibre custom column.
    *   Resolve absolute paths.
3.  **FUSE Integration (`virtual_fs.py`):**
    *   Import `VirtualZipMapper` from our generic `virtual_zip.py` module.
    *   Update `KomgaFuse._build_tree()` to instantiate the mapper for any `.epub` that has associated audio data in the DB.
    *   Update `KomgaFuse.getattr()` and `KomgaFuse.read()` to delegate directly to the `VirtualZipMapper` for these specific files.
