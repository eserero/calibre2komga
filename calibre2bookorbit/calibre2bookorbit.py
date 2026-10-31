#!/usr/bin/env python3
"""Read-only Calibre + native audiobook view for BookOrbit. Python 3.9+.
Preview needs only Python; mounting needs Linux and fusepy/libfuse.
No metadata conversion or source writes. Independent of the other mount scripts.
"""
from contextlib import closing
import argparse
import errno
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import time

LOG = logging.getLogger('calibre2bookorbit')
AUDIO_EXTS = {'.mp3', '.m4a', '.m4b', '.opus', '.ogg', '.flac'}

def relative(value):
    value = str(value).replace('\\', '/')
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(x in ('.', '..') or ':' in x for x in p.parts):
        raise ValueError('Not a safe relative path: ' + repr(value))
    return p


def contained(root, rel):
    path = root.joinpath(*relative(rel).parts).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError('Path escapes its source root: ' + str(rel))
    return path


def read_books(library, column):
    db = library / 'metadata.db'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as c:
        c.execute('BEGIN')  # consistent snapshot, including concurrent WAL updates
        col = c.execute('SELECT id FROM custom_columns WHERE label=?', (column.lstrip('#'),)).fetchone()
        if not col:
            raise ValueError('Custom column not found: ' + column)
        cid = int(col[0])
        table = 'custom_column_' + str(cid)
        fields = {r[1] for r in c.execute('PRAGMA table_info(' + table + ')')}
        if 'book' in fields:
            query = 'SELECT book,value FROM ' + table
        else:
            query = ('SELECT l.book,v.value FROM books_custom_column_' + str(cid) +
                     '_link l JOIN ' + table + ' v ON v.id=l.value ORDER BY l.id')
        audio = {}
        for bid, value in c.execute(query):
            for line in str(value or '').splitlines():
                line = line.strip()
                if line and line not in audio.setdefault(bid, []):
                    audio[bid].append(line)
        return [dict(id=r[0], title=r[1], path=r[2], audio=audio.get(r[0], []))
                for r in c.execute('SELECT id,title,path FROM books ORDER BY id')]


def build_plan(library, audio_root, column='audiobookfiles', ids=None):
    library, audio_root = Path(library).resolve(), Path(audio_root).resolve()
    if not library.is_dir() or not audio_root.is_dir():
        raise ValueError('Both source directories must exist.')
    entries, warnings, names = [], [], set()
    books = read_books(library, column)
    for book_index, book in enumerate(books, 1):
        if book_index % 500 == 0:
            LOG.info('Planning folders: %d/%d Calibre records', book_index, len(books))
        if ids and book['id'] not in ids:
            continue
        try:
            rel = relative(book['path'])
            source = contained(library, rel.as_posix())
        except ValueError as e:
            warnings.append(f"#{book['id']}: {e}")
            continue
        paths = book['audio'] or [None]
        source_exists = source.is_dir()
        for index, audio_rel in enumerate(paths, 1):
            audio = None
            if audio_rel:
                try:
                    audio = contained(audio_root, audio_rel)
                    if not audio.is_dir():
                        raise ValueError('Audio directory is missing: ' + audio_rel)
                except ValueError as e:
                    warnings.append(f"#{book['id']}: {e}")
                    # Keep the ebook entry but never silently substitute another audio edition.
                    audio = None
            if not source_exists and audio is None:
                warnings.append(f"#{book['id']}: neither book directory nor usable audio exists")
                continue
            if not source_exists:
                warnings.append(f"#{book['id']}: missing book directory; exposing audio only")
            dest = rel.as_posix()
            if len(paths) > 1:
                # Ordinal preserves database line order; source label distinguishes narrations.
                label = relative(audio_rel).name if audio_rel else 'Audio'
                label = ''.join('_' if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in label).strip('. ')
                label = label[:70] or 'Audio'
                dest = str(rel.parent / (rel.name[:120] + f' - Audio {index:02d} - ' + label))
            key = dest.casefold()
            if key in names:
                raise ValueError('Virtual folder collision: ' + dest)
            names.add(key)
            entries.append(dict(id=book['id'], title=book['title'], virtual=dest,
                                book_source=str(source) if source_exists else None,
                                audio_source=str(audio) if audio else None,
                                audio_relative=audio_rel))
    return dict(calibre_root=str(library), audio_root=str(audio_root), entries=entries,
                warnings=warnings, book_records=len({x['id'] for x in entries}),
                virtual_books=len(entries), audio_entries=sum(x['audio_source'] is not None for x in entries))


def natural_key(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', value))


class ReadOnlyView:
    """Merge each recording's audio into the book directory without copying files."""
    def __init__(self, plan):
        self.entries = {e['virtual']: e for e in plan['entries']}
        self.dirs = {'': set()}
        self.audio = {}
        self.started = time.time()
        started = time.perf_counter()
        tracks = total_bytes = 0
        for entry_index, entry in enumerate(self.entries.values(), 1):
            if entry_index % 500 == 0:
                LOG.info('Indexing audio: %d/%d virtual books; %d tracks',
                         entry_index, len(self.entries), tracks)
            name = entry['virtual']
            parts = name.split('/')
            for i, part in enumerate(parts):
                parent = '/'.join(parts[:i])
                self.dirs.setdefault(parent, set()).add(part)
                if i < len(parts) - 1:
                    self.dirs.setdefault('/'.join(parts[:i + 1]), set())
            source = Path(entry['book_source']) if entry['book_source'] else None
            occupied = {p.name.casefold() for p in source.iterdir()} if source else set()
            root = Path(entry['audio_source']) if entry['audio_source'] else None
            files = []
            if root:
                for directory, subdirs, filenames in os.walk(root, followlinks=False):
                    subdirs[:] = [d for d in subdirs if not (Path(directory) / d).is_symlink()]
                    for filename in filenames:
                        path = Path(directory) / filename
                        # Merge audio only: companion EPUB/PDF files (including
                        # those in nested discs) must come from Calibre instead.
                        if path.suffix.lower() not in AUDIO_EXTS:
                            continue
                        rel = path.relative_to(root).as_posix()
                        try:
                            safe = contained(root, rel)
                            if safe.is_file():
                                files.append((rel, safe.stat().st_size))
                        except (ValueError, OSError) as error:
                            plan['warnings'].append(f"#{entry['id']}: skipped audio {rel}: {error}")
            files.sort(key=lambda item: (natural_key(item[0]), item[0]))
            nested = any('/' in rel for rel, _ in files)
            names = {}
            for index, (rel, size) in enumerate(files, 1):
                # Number every track when flattening to preserve disc/track playback order.
                filename = PurePosixPath(rel).name
                if nested or filename.casefold() in occupied:
                    filename = f'Audio {index:06d} - ' + filename
                while filename.casefold() in occupied:
                    filename = 'Audio - ' + filename
                # Leave room for scanner operations and stay below Linux NAME_MAX.
                if len(filename.encode('utf-8')) > 240:
                    suffix = PurePosixPath(filename).suffix
                    stem = PurePosixPath(filename).stem.encode('utf-8')[:180].decode('utf-8', errors='ignore')
                    filename = stem + f' - {index:06d}' + suffix
                    if filename.casefold() in occupied:
                        raise ValueError('Virtual audio filename collision: ' + filename)
                occupied.add(filename.casefold())
                names[filename] = (root, rel)
                tracks += 1
                total_bytes += size
            self.audio[name] = names
        if set(self.entries) & set(self.dirs):
            raise ValueError('A virtual book folder overlaps another book hierarchy')
        self.stats = dict(audio_tracks=tracks, referenced_audio_bytes=total_bytes,
                          audio_preparation_seconds=round(time.perf_counter() - started, 3),
                          generated_metadata_bytes=0)
        LOG.info('Audio view ready: %d tracks; %.2f GiB referenced (not copied); %.3f seconds',
                 tracks, total_bytes / 1073741824, self.stats['audio_preparation_seconds'])

    def locate(self, path):
        parts = path.strip('/').split('/') if path.strip('/') else []
        if any(p in ('.', '..') for p in parts):
            raise OSError(errno.ENOENT, path)
        key = '/'.join(parts)
        if key in self.dirs:
            return 'directory', key
        for n in range(len(parts), 0, -1):
            owner = '/'.join(parts[:n])
            entry = self.entries.get(owner)
            if entry is None:
                continue
            tail = parts[n:]
            if not tail:
                return 'book', entry
            if len(tail) == 1 and tail[0] in self.audio[owner]:
                root, rel = self.audio[owner][tail[0]]
            elif entry['book_source']:
                root, rel = Path(entry['book_source']), '/'.join(tail)
            else:
                raise OSError(errno.ENOENT, path)
            try:
                real = contained(root, rel)
            except ValueError:
                raise OSError(errno.EACCES, path)
            if not real.exists() or not (real.is_file() or real.is_dir()):
                raise OSError(errno.ENOENT, path)
            return 'source', real
        raise OSError(errno.ENOENT, path)

    def getattr(self, path, fh=None):
        kind, node = self.locate(path)
        if kind != 'source':
            return dict(st_mode=stat.S_IFDIR | 0o555, st_nlink=2, st_size=0,
                        st_mtime=self.started, st_ctime=self.started, st_atime=self.started)
        info = node.stat()
        return dict(st_mode=(stat.S_IFDIR | 0o555) if node.is_dir() else (stat.S_IFREG | 0o444),
                    st_nlink=2 if node.is_dir() else 1, st_size=info.st_size,
                    st_mtime=info.st_mtime, st_ctime=info.st_ctime, st_atime=info.st_atime)

    def readdir(self, path, fh=None):
        kind, node = self.locate(path)
        if kind == 'directory':
            names = self.dirs[node]
        elif kind == 'book':
            names = set(self.audio[node['virtual']])
            if node['book_source']:
                names.update(p.name for p in Path(node['book_source']).iterdir())
        elif node.is_dir():
            names = {p.name for p in node.iterdir()}
        else:
            raise OSError(errno.ENOTDIR, path)
        return ['.', '..'] + sorted(names)

    def open(self, path, flags):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            raise OSError(errno.EROFS, path)
        kind, node = self.locate(path)
        if kind != 'source' or not node.is_file():
            raise OSError(errno.EISDIR, path)
        return os.open(node, os.O_RDONLY | getattr(os, 'O_BINARY', 0))

    def read(self, path, size, offset, fh):
        if hasattr(os, 'pread'):
            return os.pread(fh, size, offset)
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def release(self, path, fh):
        os.close(fh)
        return 0

    def access(self, path, mode):
        self.locate(path)
        if mode & os.W_OK:
            raise OSError(errno.EROFS, path)
        return 0


def mount(plan, mountpoint, allow_other, view=None):
    if not sys.platform.startswith('linux'):
        raise ValueError('Mounting requires Linux/FUSE. Use preview on Windows.')
    try:
        try:
            from fuse import FUSE, Operations, FuseOSError
        except ModuleNotFoundError as missing:
            if missing.name != 'fuse':
                raise
            # Debian/Ubuntu python3-fusepy uses a different module name.
            from fusepy import FUSE, Operations, FuseOSError
    except (ImportError, OSError) as e:
        raise ValueError('Install fusepy and the system libfuse library: ' + str(e))
    target = Path(mountpoint).resolve()
    for root in (Path(plan['calibre_root']), Path(plan['audio_root'])):
        if target == root or root in target.parents or target in root.parents:
            raise ValueError('Mount point must be separate from both source roots.')
    target.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(target) or any(target.iterdir()):
        raise ValueError('Mount point must be empty and not already mounted.')
    view = view if view is not None else ReadOnlyView(plan)
    def wrap(name):
        def operation(self, *args):
            try:
                return getattr(view, name)(*args)
            except OSError as e:
                raise FuseOSError(e.errno or errno.EIO)
        return operation
    def deny(self, *args):
        raise FuseOSError(errno.EROFS)
    methods = {n: wrap(n) for n in ('getattr', 'readdir', 'open', 'read', 'release', 'access')}
    methods.update({n: deny for n in ('write', 'create', 'unlink', 'rmdir', 'mkdir', 'rename',
                                    'truncate', 'chmod', 'chown', 'utimens', 'symlink', 'link', 'mknod')})
    methods['init'] = lambda self, path: LOG.info('FUSE mount ready: %s', target)
    cls = type('BookOrbitReadOnlyFS', (Operations,), methods)
    FUSE(cls(), str(target), foreground=True, nothreads=True, ro=True,
         allow_other=allow_other, fsname='calibre2bookorbit')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preview', 'mount'])
    parser.add_argument('--calibre', required=True, help='Calibre library directory containing metadata.db')
    parser.add_argument('--audio-base', required=True)
    parser.add_argument('--audiobook-column', default='audiobookfiles')
    parser.add_argument('--book-id', type=int, action='append', help='Limit to a Calibre ID; repeatable')
    parser.add_argument('--output', help='Preview JSON file; omitted means print JSON')
    parser.add_argument('--mountpoint')
    parser.add_argument('--allow-other', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    try:
        started = time.perf_counter()
        LOG.info('Preparing virtual library')
        if args.command == 'mount' and not args.mountpoint:
            parser.error('mount requires --mountpoint')
        plan = build_plan(args.calibre, args.audio_base, args.audiobook_column, set(args.book_id or []))
        LOG.info('Folder planning: %.3f seconds', time.perf_counter() - started)
        view = ReadOnlyView(plan)
        plan['view_summary'] = view.stats
        plan['audio_files'] = {key: {name: rel for name, (_, rel) in files.items()}
                               for key, files in view.audio.items()}
        LOG.info('Total startup preparation: %.3f seconds', time.perf_counter() - started)
        print(f"{plan['book_records']} records; {plan['virtual_books']} virtual book folders; "
              f"{plan['audio_entries']} with audio; {len(plan['warnings'])} folder-mapping warnings", file=sys.stderr)
        if args.command == 'preview':
            data = json.dumps(plan, ensure_ascii=False, indent=2)
            if args.output:
                dest = Path(args.output).resolve()
                for source in (Path(plan['calibre_root']), Path(plan['audio_root'])):
                    if dest == source or source in dest.parents:
                        raise ValueError('Preview output cannot be written inside a source library.')
                dest.write_text(data + '\n', encoding='utf-8')
            else:
                print(data)
        else:
            for warning in plan['warnings']:
                print('WARNING: ' + warning, file=sys.stderr)
            mount(plan, args.mountpoint, args.allow_other, view)
    except (ValueError, OSError, sqlite3.Error) as e:
        print('ERROR: ' + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
