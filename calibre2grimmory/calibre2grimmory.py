#!/usr/bin/env python3
"""Standalone read-only Calibre + native audiobook view. Python 3.9+.
Preview uses only the standard library. Linux mounting requires fusepy/libfuse.
No imports from, or changes to, calibre2komga. No source files are written.
"""
from contextlib import closing
import argparse
import errno
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path, PurePosixPath


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
    for book in read_books(library, column):
        if ids and book['id'] not in ids:
            continue
        try:
            rel = relative(book['path'])
            source = contained(library, rel.as_posix())
        except ValueError as e:
            warnings.append(f"#{book['id']}: {e}")
            continue
        paths = book['audio'] or [None]
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
            if not source.is_dir() and audio is None:
                warnings.append(f"#{book['id']}: neither book directory nor usable audio exists")
                continue
            if not source.is_dir():
                warnings.append(f"#{book['id']}: missing book directory; exposing audio only")
            if source.is_dir() and (source / 'audiobook').exists():
                raise ValueError(f"#{book['id']}: existing 'audiobook' item conflicts with the virtual folder")
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
                                book_source=str(source) if source.is_dir() else None,
                                audio_source=str(audio) if audio else None,
                                audio_relative=audio_rel))
    return dict(calibre_root=str(library), audio_root=str(audio_root), entries=entries,
                warnings=warnings, book_records=len({x['id'] for x in entries}),
                virtual_books=len(entries), audio_entries=sum(x['audio_source'] is not None for x in entries))


class ReadOnlyView:
    """FUSE-independent operations, also exercised by the tests on Windows."""
    def __init__(self, plan):
        self.entries = {e['virtual']: e for e in plan['entries']}
        self.dirs = {'': set()}
        self.started = time.time()
        for name in self.entries:
            parts = name.split('/')
            for i, part in enumerate(parts):
                parent = '/'.join(parts[:i])
                self.dirs.setdefault(parent, set()).add(part)
                if i < len(parts) - 1:
                    self.dirs.setdefault('/'.join(parts[:i + 1]), set())
        if set(self.entries) & set(self.dirs):
            raise ValueError('A virtual book folder overlaps another book hierarchy')

    def locate(self, path):
        parts = path.strip('/').split('/') if path.strip('/') else []
        if any(p in ('.', '..') for p in parts):
            raise OSError(errno.ENOENT, path)
        key = '/'.join(parts)
        if key in self.dirs:
            return ('directory', key, None)
        for n in range(len(parts), 0, -1):
            entry = self.entries.get('/'.join(parts[:n]))
            if entry is None:
                continue
            tail = parts[n:]
            if not tail:
                return ('book', entry, None)
            root = entry['book_source']
            if tail[0] == 'audiobook':
                root = entry['audio_source']
                tail = tail[1:]
            if root is None:
                raise OSError(errno.ENOENT, path)
            root = Path(root)
            try:
                real = contained(root, '/'.join(tail)) if tail else root
            except ValueError:
                raise OSError(errno.EACCES, path)
            if not real.exists() or not (real.is_file() or real.is_dir()):
                raise OSError(errno.ENOENT, path)
            return ('source', real, root)
        raise OSError(errno.ENOENT, path)

    def getattr(self, path, fh=None):
        kind, node, _ = self.locate(path)
        if kind != 'source':
            return dict(st_mode=stat.S_IFDIR | 0o555, st_nlink=2, st_size=0,
                        st_mtime=self.started, st_ctime=self.started, st_atime=self.started)
        s = node.stat()
        return dict(st_mode=(stat.S_IFDIR | 0o555) if node.is_dir() else (stat.S_IFREG | 0o444),
                    st_nlink=2 if node.is_dir() else 1, st_size=s.st_size,
                    st_mtime=s.st_mtime, st_ctime=s.st_ctime, st_atime=s.st_atime)

    def readdir(self, path, fh=None):
        kind, node, _ = self.locate(path)
        if kind == 'directory':
            names = self.dirs[node]
        elif kind == 'book':
            names = set()
            if node['book_source']:
                names.update(x.name for x in Path(node['book_source']).iterdir())
            if node['audio_source']:
                names.add('audiobook')
        else:
            if not node.is_dir():
                raise OSError(errno.ENOTDIR, path)
            names = {x.name for x in node.iterdir()}
        return ['.', '..'] + sorted(names)

    def open(self, path, flags):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            raise OSError(errno.EROFS, path)
        kind, node, _ = self.locate(path)
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


def mount(plan, mountpoint, allow_other):
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
        raise ValueError('Install fusepy and the system libfuse library in a separate environment: ' + str(e))
    target = Path(mountpoint).resolve()
    for root in (Path(plan['calibre_root']), Path(plan['audio_root'])):
        if target == root or root in target.parents or target in root.parents:
            raise ValueError('Mount point must be separate from both source roots.')
    target.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(target) or any(target.iterdir()):
        raise ValueError('Mount point must be empty and not already mounted.')
    view = ReadOnlyView(plan)
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
    cls = type('GrimmoryReadOnlyFS', (Operations,), methods)
    FUSE(cls(), str(target), foreground=True, nothreads=True, ro=True,
         allow_other=allow_other, fsname='calibre2grimmory')


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
    try:
        if args.command == 'mount' and not args.mountpoint:
            parser.error('mount requires --mountpoint')
        plan = build_plan(args.calibre, args.audio_base, args.audiobook_column, set(args.book_id or []))
        ReadOnlyView(plan)  # validate hierarchy before reporting/mounting
        print(f"{plan['book_records']} records; {plan['virtual_books']} virtual book folders; "
              f"{plan['audio_entries']} with audio; {len(plan['warnings'])} warnings", file=sys.stderr)
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
            mount(plan, args.mountpoint, args.allow_other)
    except (ValueError, OSError, sqlite3.Error) as e:
        print('ERROR: ' + str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
