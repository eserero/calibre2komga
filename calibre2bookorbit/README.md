# calibre2bookorbit

Independent, read-only FUSE view for BookOrbit. Uses Calibre's existing author/book hierarchy and `audiobookfiles` custom column. It does not import or change either of the other mount scripts. Python 3.9+; Linux/FUSE for mounting. Preview works on Windows too.

## Layout

```text
Author/
  Book (123)/
    Book.epub
    Book.pdf
    metadata.opf
    cover.jpg
    01.mp3
    02.mp3
```

Audio files appear directly beside the ebooks. Calibre files, including the original OPF and cover, pass through without conversion. EPUB and PDF files found in audiobook folders are excluded, including uppercase extensions and files in nested folders; ebooks come from Calibre only. Audio folder covers, OPFs, JSON and other non-audio files are also excluded so they cannot override Calibre metadata. No generated JSON, cache or separate metadata importer is needed for this view.

Multiple nonempty lines in `audiobookfiles` produce separate full virtual book folders, named `Book (123) - Audio 01 - <recording label>`, etc. Each includes the ebook files and one recording. Paths in the column are relative to `--audio-base`; Windows backslashes are accepted on Linux.

Nested audio directories are flattened **virtually**. Tracks are sorted by relative path with natural numeric ordering (`Disc 2` before `Disc 10`, `2.mp3` before `10.mp3`), then exposed as `Audio 000001 - <original filename>`, etc. This keeps identical filenames from different discs distinct and avoids BookOrbit interpreting arbitrary audio subfolders as separate books. Playback follows folder/filename ordering, not embedded track tags; check the preview for unusually named folders. Flat audio filenames stay unchanged unless they collide with a Calibre item. A collision gets an audio prefix; source files are never hidden or overwritten. Audio extensions: MP3, M4A, M4B, OPUS, OGG, FLAC.

Missing audio produces warnings while retaining the ebook. Missing Calibre book folders can expose audio only, with a warning and no Calibre OPF. Other existing Calibre subdirectories pass through unchanged. Refresh the mount after changing Calibre records or recording contents.

## Install

On Ubuntu/Debian with system Python (no venv required):

```bash
sudo apt update
sudo apt install python3-fusepy
python3 -c "import fusepy; print('FUSE ready')"
```

The script accepts the pip `fuse` and apt `fusepy` module names. For pip installations use `requirements.txt` in your chosen environment and install the system libfuse library. To use `--allow-other`, enable `user_allow_other` in `/etc/fuse.conf`.

## Preview and mount

Run from the repository root:

```bash
python3 calibre2bookorbit/calibre2bookorbit.py preview \
  --calibre "/path/to/Caliber" \
  --audio-base "/path/to/Audiobooks" \
  --output bookorbit-preview.json
```

The preview includes all folder mappings, audio virtual-name/source-path mappings, warnings, track count, referenced audio size and preparation time. Use repeatable `--book-id 13007` to try selected Calibre records. Both source roots must exist. The custom column defaults to `audiobookfiles`; use `--audiobook-column` if needed.

```bash
mkdir -p "$HOME/calibre2komga/bookorbit_virtual"
nohup python3 calibre2bookorbit/calibre2bookorbit.py mount \
  --calibre "/home/eyal/Videos/ext_exfat_USB_Drive/My Data/Media/books/Caliber/" \
  --audio-base "/home/eyal/Videos/ext_exfat_USB_Drive/My Data/Media/Audio/Updated Audio Books/" \
  --mountpoint "$HOME/calibre2komga/bookorbit_virtual" \
  --allow-other \
  --audiobook-column audiobookfiles \
  > bookorbit_mount.log 2>&1 &
```

Use a separate empty, unmounted mountpoint outside the source trees. Unmount before replacing it:

```bash
fusermount -u "$HOME/calibre2komga/bookorbit_virtual"
```

Logs show planning progress every 500 Calibre records, audio track count and referenced audio bytes, preparation timings, warnings and mount readiness. Referenced size is not additional disk usage: media are read from their original locations. No OPF conversion is performed.

## BookOrbit setup

1. Mount this view before starting BookOrbit. For Docker bind it read-only, for example `/home/eyal/calibre2komga/bookorbit_virtual:/books:ro`.
2. Create the library using **Folder as Book**, pointing at `/books`. Enable the ebook/audio formats you want imported.
3. In metadata settings, put OPF sidecar metadata first in **Book details come from**. Keep file moving, renaming and metadata writing disabled for the read-only view.
4. Scan a few sample books first, including a PDF, a multi-track recording and multiple editions. A combined folder should become one record with the available ebook and audio formats.
5. After remounting the host view, restart the BookOrbit container to refresh its Docker bind mount, then run a manual scan. OPF-only changes require a manual scan according to the documentation.

The layout follows [BookOrbit's documented folder/sidecar rules](https://bookorbit.app/library-file-structure) and [library setup](https://bookorbit.app/creating-a-library). The user has confirmed the mount is working with BookOrbit. Thirteen local tests also cover filesystem behavior, preview and FUSE wiring. Native OPF support does not guarantee that every Calibre custom field is imported; in particular this script does not translate secondary-series/reading-list custom fields into tags.

## Tests

```bash
python3 calibre2bookorbit/test_calibre2bookorbit.py
```

Tests cover OPF/source preservation, nested-disc ordering, duplicate filenames, multiple recordings, missing sources, column schemas, format selection, random reads and read-only/path protections. Mounting itself requires a Linux test.
