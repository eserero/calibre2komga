# calibre2grimmory

A standalone, read-only FUSE view of a Calibre library with native audiobooks for Grimmory. This directory is independent of calibre2komga: it imports no Komga modules, uses a separate mount point and does not change Komga's configuration or dependencies.

This is the working native-audio baseline. Virtual JSON metadata sidecars are **not implemented** in this version.

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
  --output preview.json
```

The preview lists each virtual folder and its original source paths, with warnings. It reads the database but does not mount anything. Preview also works on Windows without FUSE; pass the actual Windows source paths. Keep the JSON private because it includes library names and paths.

For a small trial, add `--book-id 123 --book-id 456` using IDs from your library. The same filter works with `mount`.

## Mount

Use a dedicated empty mount point, separate from Komga and both source directories:

```bash
nohup python3 calibre2grimmory.py mount \
  --calibre /path/to/calibre \
  --audio-base /path/to/audiobooks \
  --mountpoint "$HOME/grimmory_virtual" \
  --audiobook-column audiobookfiles \
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

### Metadata limitation

Existing `metadata.opf` and cover files are exposed unchanged. This baseline does not generate Grimmory JSON sidecars or change embedded metadata. **Do not assume Prefer Sidecar imports Calibre's OPF or its series fields correctly.** Prefer Sidecar falls back to embedded metadata when no supported sidecar is found. Verify series and series positions in your installed Grimmory release.

References:
- [Grimmory issue #2466: Calibre OPF series import](https://github.com/grimmory-tools/grimmory/issues/2466)
- [Adjacent OPF import proposal #867 (closed without merge)](https://github.com/grimmory-tools/grimmory/pull/867)

Generated JSON sidecars are a possible future enhancement, deliberately excluded from this baseline.

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
- No automatic conversion, metadata enrichment, grouping of duplicate Calibre records, or source cleanup is performed.
- Mapping is built at startup; restart the mount after association/path changes, then rescan Grimmory. FUSE may not emit watcher events for changes to the underlying files. Stop consumers before remounting.
- File reads, including seeking, are direct. Database/media writes through the mount are prohibited. Out-of-root symlink targets cannot be opened.

## Tests and validation

```bash
python3 test_calibre2grimmory.py
```

Nine tests cover multiline entries, original bytes and offset reads, read-only database access, nested audio, audio-only and ebook-only records, rejected writes, path traversal, missing sources, custom-column storage layouts and name conflicts.

The baseline was tested against a live Windows library for preview and byte reads. The user subsequently confirmed a successful Linux FUSE mount. Grimmory metadata import has the limitation described above; complete scan/playback compatibility is not asserted.
