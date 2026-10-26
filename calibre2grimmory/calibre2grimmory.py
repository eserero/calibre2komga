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
import re
import logging
import hashlib
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath


LOG = logging.getLogger('calibre2grimmory')
BOOK_EXTS = {'.epub', '.pdf', '.cbz', '.cbr', '.cb7', '.mobi', '.azw3', '.fb2'}
AUDIO_EXTS = {'.mp3', '.m4a', '.m4b'}
CACHE_VERSION = 2  # Increase when OPF mapping or cache schema changes.


class CachedPayload:
    """A standard JSON cache file, read and verified only when a reader opens it."""
    def __init__(self, cache, opf, target, record, data=None):
        self.cache, self.opf, self.target, self.record = cache, opf, target, record
        self.data = data

    def __len__(self):
        return self.record['size']

    def materialize(self):
        if self.data is None:
            try:
                data = self.target.read_bytes()
                if hashlib.sha256(data).hexdigest() != self.record['sha256']:
                    raise ValueError('cache checksum mismatch')
                self.data = data
            except (OSError, ValueError):
                LOG.warning('Repairing missing/corrupt cached JSON for %s', self.opf)
                s = self.opf.stat()
                if [CACHE_VERSION, s.st_mtime_ns, s.st_ctime_ns, s.st_size] != self.record['signature']:
                    raise OSError(errno.EIO, 'OPF changed since startup; restart the mount to refresh metadata')
                # Recreate the same payload, including its original generatedAt value.
                try:
                    self.data = self.cache.convert(self.opf, self.record['signature'],
                                                   self.record['generated_at']).data
                except (ValueError, ET.ParseError) as error:
                    raise OSError(errno.EIO, str(error)) from error
                self.cache.save()
        return self.data

    def __getitem__(self, key):
        return self.materialize()[key]


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.c2g-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SidecarCache:
    """Persistent JSON payloads with stat-based invalidation and corruption detection."""
    def __init__(self, directory, sources):
        self.root = Path(directory).resolve()
        for source in sources:
            source = Path(source).resolve()
            if self.root == source or source in self.root.parents or self.root in source.parents:
                raise ValueError('Sidecar cache must be separate from source roots and mountpoint.')
        marker = self.root / '.calibre2grimmory-cache'
        if self.root.exists() and any(self.root.iterdir()) and not marker.is_file():
            raise ValueError('Use an empty dedicated directory for the sidecar cache.')
        self.root.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            atomic_write(marker, b'calibre2grimmory sidecar cache\n')
        self.index_path = self.root / 'index.json'
        try:
            self.index = json.loads(self.index_path.read_text(encoding='utf-8'))
            if not isinstance(self.index, dict):
                raise ValueError('invalid cache index')
        except (OSError, ValueError):
            self.index = {}
        self.hits = self.rebuilt = self.input_bytes = 0
        self.dirty = False

    def get(self, opf):
        s = opf.stat()
        signature = [CACHE_VERSION, s.st_mtime_ns, s.st_ctime_ns, s.st_size]
        key = hashlib.sha256(str(opf).encode('utf-8')).hexdigest()
        target = self.root / (key + '.metadata.json')
        record = self.index.get(key)
        if isinstance(record, dict) and record.get('signature') == signature:
            if (isinstance(record.get('size'), int) and isinstance(record.get('mtime'), (int, float))
                    and isinstance(record.get('generated_at'), str) and isinstance(record.get('sha256'), str)):
                self.hits += 1
                return CachedPayload(self, opf, target, record)
        return self.convert(opf, signature)

    def convert(self, opf, signature, generated_at=None):
        key = hashlib.sha256(str(opf).encode('utf-8')).hexdigest()
        target = self.root / (key + '.metadata.json')
        metadata = opf_metadata(opf)
        after = opf.stat()
        if [CACHE_VERSION, after.st_mtime_ns, after.st_ctime_ns, after.st_size] != signature:
            raise ValueError('OPF changed while being converted; retry after Calibre finishes saving')
        generated_at = generated_at or datetime.now(timezone.utc).isoformat()
        data = json.dumps(dict(version='1.0', generatedAt=generated_at,
                               generatedBy='calibre2grimmory', metadata=metadata),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
        atomic_write(target, data)
        record = dict(signature=signature, sha256=hashlib.sha256(data).hexdigest(), source=str(opf),
                      size=len(data), mtime=target.stat().st_mtime, generated_at=generated_at)
        self.index[key] = record
        self.dirty = True
        self.rebuilt += 1
        self.input_bytes += after.st_size
        return CachedPayload(self, opf, target, record, data)

    def save(self):
        if self.dirty:
            atomic_write(self.index_path, json.dumps(self.index, ensure_ascii=False).encode('utf-8'))
            self.dirty = False


def opf_metadata(path):
    """Convert explicit OPF values only; never modify the source or infer missing data."""
    root = ET.parse(path).getroot()
    ns = {'dc': 'http://purl.org/dc/elements/1.1/', 'o': 'http://www.idpf.org/2007/opf'}
    md = root.find('o:metadata', ns)
    if md is None:
        raise ValueError('OPF has no metadata element')
    def values(field):
        return [''.join(e.itertext()).strip() for e in md.findall('dc:' + field, ns)
                if ''.join(e.itertext()).strip()]
    meta = {e.get('name'): e.get('content') for e in md.findall('o:meta', ns)}
    custom = {}
    for key, value in meta.items():
        if key and key.startswith('calibre:user_metadata:'):
            try:
                custom[key.split('#', 1)[-1]] = json.loads(value).get('#value#')
            except (ValueError, TypeError, AttributeError):
                LOG.warning('%s: invalid custom field %s', path, key)
    out = {}
    for src, dst in [('title', 'title'), ('publisher', 'publisher'), ('description', 'description')]:
        v = values(src)
        if v:
            out[dst] = v[0]
    authors = [''.join(e.itertext()).strip() for e in md.findall('dc:creator', ns)
               if e.get('{%s}role' % ns['o'], 'aut') == 'aut']
    if any(authors):
        out['authors'] = list(dict.fromkeys(a for a in authors if a))
    language = values('language')
    if language:
        # Keep valid three-letter codes when no unambiguous short alias is listed.
        aliases = dict(eng='en', heb='he', fra='fr', fre='fr', deu='de', ger='de',
                       spa='es', ita='it', rus='ru', por='pt', jpn='ja', zho='zh', chi='zh')
        out['language'] = aliases.get(language[0].lower(), language[0].lower())
    dates = values('date')
    if dates:
        try:
            d = date.fromisoformat(dates[0][:10])
            if d.year > 101:
                out['publishedDate'] = d.isoformat()
        except ValueError:
            pass
    series = meta.get('calibre:series')
    if series:
        out['series'] = {'name': series}
        number = meta.get('calibre:series_index') or ''
        if re.fullmatch(r'-?\d+(\.\d+)?', number):
            out['series']['number'] = float(number)
    def strings(v):
        return [str(x).strip() for x in (v if isinstance(v, list) else [v])
                if x is not None and str(x).strip()]
    tags = values('subject')
    for field, prefix in [('series2', 'Secondary series: '), ('readinglist', 'Reading list: ')]:
        tags.extend(prefix + v for v in strings(custom.get(field)))
    if tags:
        out['tags'] = list(dict.fromkeys(tags))
    categories = strings(custom.get('category'))
    if categories:
        out['categories'] = categories
    pages = custom.get('pages')
    if isinstance(pages, int) and not isinstance(pages, bool) and pages > 0:
        out['pageCount'] = pages
    narrators = strings(custom.get('abs_narrator'))
    if narrators:
        out['narrator'] = ' & '.join(narrators)
    identifiers = {}
    schemes = dict(amazon='asin', asin='asin', goodreads='goodreadsId', google='googleId',
                   hardcover='hardcoverId', hardcover_book='hardcoverBookId',
                   comicvine='comicvineId', lubimyczytac='lubimyczytacId',
                   ranobedb='ranobedbId', audible='audibleId', applebooks='applebooksId',
                   openlibrary='openlibraryId')
    for e in md.findall('dc:identifier', ns):
        scheme = e.get('{%s}scheme' % ns['o'], '').lower()
        value = (e.text or '').strip()
        if not value:
            continue
        if scheme in ('isbn', 'isbn10', 'isbn13'):
            isbn = re.sub(r'[\s-]', '', value).upper()
            valid10 = (re.fullmatch(r'\d{9}[\dX]', isbn) and
                       sum((10-i) * (10 if c == 'X' else int(c)) for i, c in enumerate(isbn)) % 11 == 0)
            valid13 = (re.fullmatch(r'97[89]\d{10}', isbn) and
                       sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn)) % 10 == 0)
            if valid10 or valid13:
                out['isbn10' if valid10 else 'isbn13'] = isbn
        elif scheme in schemes:
            identifiers[schemes[scheme]] = value
    if identifiers:
        out['identifiers'] = identifiers
    return out


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
        if source_exists and (source / 'audiobook').exists():
            raise ValueError(f"#{book['id']}: existing 'audiobook' item conflicts with the virtual folder")
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


class ReadOnlyView:
    """FUSE-independent operations, also exercised by the tests on Windows."""
    def __init__(self, plan, sidecars=True, disk_cache=None):
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
        self.generated, self.covers, self.extra_names = {}, {}, {}
        self.handles, self.next_handle = {}, 1 << 62
        self.stats = {}
        self.disk_cache = disk_cache
        self.lazy_metadata, self.prepared_dirs = {}, set()
        if sidecars:
            self.prepare_sidecars()

    def prepare_sidecars(self):
        start = time.perf_counter()
        cache, failures, opf_bytes, overlays = {}, 0, 0, 0
        generated_at = datetime.now(timezone.utc).isoformat()
        def register(key, value, table):
            table[key] = value
            parent, name = key.rsplit('/', 1)
            self.extra_names.setdefault(parent, set()).add(name)
        for index, entry in enumerate(self.entries.values(), 1):
            source = entry['book_source']
            if source not in cache:
                cache[source] = None
                try:
                    if not source:
                        raise ValueError('no Calibre folder containing metadata.opf')
                    # The source directory is already resolved and checked by build_plan.
                    # Resolve again only for an OPF symlink, avoiding ancestor traversal per book.
                    opf = Path(source) / 'metadata.opf'
                    if opf.is_symlink():
                        opf = contained(Path(source), 'metadata.opf')
                    if self.disk_cache:
                        cache[source] = self.disk_cache.get(opf)
                    else:
                        opf_bytes += opf.stat().st_size
                        metadata = opf_metadata(opf)
                        payload = dict(version='1.0', generatedAt=generated_at,
                                       generatedBy='calibre2grimmory', metadata=metadata)
                        cache[source] = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
                except (OSError, ValueError, ET.ParseError) as e:
                    failures += 1
                    LOG.warning('#%s: no generated sidecar: %s', entry['id'], e)
            payload = cache[source]
            if payload is None:
                continue
            if self.disk_cache:
                self.lazy_metadata[entry['virtual']] = payload
                if index % 500 == 0:
                    LOG.info('Checking metadata cache: %d/%d virtual folders', index, len(self.entries))
                continue
            cover = None
            try:
                candidate = contained(Path(source), 'cover.jpg')
                if candidate.is_file():
                    cover = candidate
            except ValueError:
                pass
            targets = []
            for root_value, segment, extensions in [(source, '', BOOK_EXTS),
                    (entry['audio_source'], '/audiobook', AUDIO_EXTS)]:
                if not root_value:
                    continue
                root = Path(root_value)
                for directory, subdirs, files in os.walk(root, followlinks=False):
                    subdirs[:] = [d for d in subdirs if not (Path(directory) / d).is_symlink()]
                    for name in files:
                        file = Path(directory) / name
                        if file.suffix.lower() not in extensions:
                            continue
                        rel = file.relative_to(root).as_posix()
                        if file.is_symlink():
                            try:
                                contained(root, rel)
                            except ValueError:
                                continue
                        virtual_file = entry['virtual'] + segment + '/' + rel
                        targets.append((virtual_file, file))
                        if segment:
                            parent_source = Path(source) / 'audiobook' if file.parent == root else file.parent
                            targets.append((str(PurePosixPath(virtual_file).parent), parent_source))
            # Grouped audiobooks use a directory as their primary file path.
            # Match Grimmory's sibling lookup for that directory as well as tracks.
            if entry['audio_source']:
                targets.append((entry['virtual'] + '/audiobook', Path(source) / 'audiobook'))
            for target, original in targets:
                p = PurePosixPath(target)
                base = str(p.with_suffix('')) if p.suffix else str(p)
                original_base = original.with_suffix('') if original.suffix else original
                for suffix, value, table in [('.metadata.json', payload, self.generated),
                                              ('.cover.jpg', cover, self.covers)]:
                    if value is None:
                        continue
                    key = base + suffix
                    if key in table:
                        continue
                    if Path(str(original_base) + suffix).exists():
                        overlays += 1
                        LOG.warning('Virtual Calibre metadata overrides existing sidecar/cover: %s '
                                    '(original file unchanged)', key)
                    register(key, value, table)
            if index % 500 == 0:
                LOG.info('Preparing metadata: %d/%d virtual book folders', index, len(self.entries))
        if self.disk_cache:
            self.disk_cache.save()
            opf_bytes = self.disk_cache.input_bytes
        self.stats = dict(opf_files=sum(v is not None for v in cache.values()),
                          opf_bytes=opf_bytes, skipped_opfs=failures,
                          sidecar_files=len(self.generated), cover_aliases=len(self.covers),
                          existing_files_overlaid=overlays,
                          cached_json_bytes=sum(len(v) for v in cache.values() if v is not None),
                          virtual_json_bytes=sum(map(len, self.generated.values())),
                          preparation_seconds=round(time.perf_counter() - start, 3))
        if self.disk_cache:
            self.stats.update(cache_hits=self.disk_cache.hits, cache_rebuilt=self.disk_cache.rebuilt,
                              aliases_on_demand=True, cache_directory=str(self.disk_cache.root))
            LOG.info('Metadata cache ready: %d reused; %d converted; %d skipped; '
                     '%.2f MiB JSON payload indexed; %.2f MiB OPF read; %.3f seconds. '
                     'Cached JSON and virtual aliases are loaded on demand; source files unchanged.',
                     self.disk_cache.hits, self.disk_cache.rebuilt, failures,
                     self.stats['cached_json_bytes'] / 1048576, opf_bytes / 1048576,
                     self.stats['preparation_seconds'])
            return
        LOG.info('Virtual folders prepared: %d folders; %d OPFs converted; %d skipped; '
                 '%d JSON sidecars; %d cover aliases; %d existing files overlaid; %.2f MiB OPF input; '
                 '%.2f MiB cached JSON; %.2f MiB exposed JSON; %.3f seconds. '
                 'No source files written; media size and Python overhead excluded.',
                 len(self.entries), self.stats['opf_files'], failures, len(self.generated),
                 len(self.covers), overlays, opf_bytes / 1048576,
                 self.stats['cached_json_bytes'] / 1048576,
                 self.stats['virtual_json_bytes'] / 1048576, self.stats['preparation_seconds'])

    def prepare_directory(self, path):
        """Discover aliases only for a directory that a reader actually visits."""
        key = path.strip('/')
        if not self.disk_cache or key in self.prepared_dirs:
            return
        parts = key.split('/')
        owner = next(('/'.join(parts[:n]) for n in range(len(parts), 0, -1)
                      if '/'.join(parts[:n]) in self.lazy_metadata), None)
        if owner is None:
            return
        kind, node, _ = self._locate(path)
        entry = self.entries[owner]
        if kind == 'book':
            root = Path(entry['book_source'])
            targets = [p for p in root.iterdir() if p.suffix.lower() in BOOK_EXTS]
            if entry['audio_source']:
                targets.append(root / 'audiobook')
        elif kind == 'source' and node.is_dir():
            root = node
            in_audio = key == owner + '/audiobook' or key.startswith(owner + '/audiobook/')
            targets = [p for p in root.iterdir() if p.suffix.lower() in (AUDIO_EXTS if in_audio else BOOK_EXTS)
                       or (in_audio and p.is_dir() and not p.is_symlink())]
        else:
            return
        try:
            cover = contained(Path(entry['book_source']), 'cover.jpg')
            cover = cover if cover.is_file() else None
        except ValueError:
            cover = None
        for target in targets:
            base = target.stem if target.suffix else target.name
            for suffix, data, table in [('.metadata.json', self.lazy_metadata[owner], self.generated),
                                        ('.cover.jpg', cover, self.covers)]:
                if data is None:
                    continue
                name = base + suffix
                virtual = key + '/' + name
                if virtual in table:
                    continue
                if (root / name).exists():
                    LOG.warning('Virtual Calibre metadata overrides %s (original unchanged)', virtual)
                table[virtual] = data
                self.extra_names.setdefault(key, set()).add(name)
        self.prepared_dirs.add(key)

    def locate(self, path):
        if path.endswith(('.metadata.json', '.cover.jpg')):
            self.prepare_directory(str(PurePosixPath(path).parent))
        return self._locate(path)

    def _locate(self, path):
        parts = path.strip('/').split('/') if path.strip('/') else []
        if any(p in ('.', '..') for p in parts):
            raise OSError(errno.ENOENT, path)
        key = '/'.join(parts)
        if key in self.generated:
            return ('generated', self.generated[key], None)
        if key in self.covers:
            real = self.covers[key]
            # Revalidate aliases in case a source was replaced by an escaping symlink.
            return ('source', contained(real.parent, real.name), real.parent)
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
        if kind == 'generated':
            timestamp = node.record['mtime'] if isinstance(node, CachedPayload) else self.started
            return dict(st_mode=stat.S_IFREG | 0o444, st_nlink=1, st_size=len(node),
                        st_mtime=timestamp, st_ctime=timestamp, st_atime=timestamp)
        if kind != 'source':
            return dict(st_mode=stat.S_IFDIR | 0o555, st_nlink=2, st_size=0,
                        st_mtime=self.started, st_ctime=self.started, st_atime=self.started)
        s = node.stat()
        return dict(st_mode=(stat.S_IFDIR | 0o555) if node.is_dir() else (stat.S_IFREG | 0o444),
                    st_nlink=2 if node.is_dir() else 1, st_size=s.st_size,
                    st_mtime=s.st_mtime, st_ctime=s.st_ctime, st_atime=s.st_atime)

    def readdir(self, path, fh=None):
        self.prepare_directory(path)
        kind, node, _ = self.locate(path)
        if kind == 'directory':
            names = self.dirs[node]
        elif kind == 'book':
            names = set()
            if node['book_source']:
                names.update(x.name for x in Path(node['book_source']).iterdir())
            if node['audio_source']:
                names.add('audiobook')
        elif kind == 'source':
            if not node.is_dir():
                raise OSError(errno.ENOTDIR, path)
            names = {x.name for x in node.iterdir()}
        else:
            raise OSError(errno.ENOTDIR, path)
        names = set(names) | self.extra_names.get(path.strip('/'), set())
        return ['.', '..'] + sorted(names)

    def open(self, path, flags):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            raise OSError(errno.EROFS, path)
        kind, node, _ = self.locate(path)
        if kind == 'generated':
            if isinstance(node, CachedPayload):
                node.materialize()
            handle = self.next_handle
            self.next_handle += 1
            self.handles[handle] = node
            return handle
        if kind != 'source' or not node.is_file():
            raise OSError(errno.EISDIR, path)
        return os.open(node, os.O_RDONLY | getattr(os, 'O_BINARY', 0))

    def read(self, path, size, offset, fh):
        if fh in self.handles:
            return self.handles[fh][offset:offset + size]
        if hasattr(os, 'pread'):
            return os.pread(fh, size, offset)
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, size)

    def release(self, path, fh):
        if fh in self.handles:
            del self.handles[fh]
            return 0
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
        raise ValueError('Install fusepy and the system libfuse library in a separate environment: ' + str(e))
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
    sidecar_options = parser.add_mutually_exclusive_group()
    sidecar_options.add_argument('--sidecar-cache', metavar='DIRECTORY',
                                 help='Enable OPF conversion with a persistent JSON cache outside source folders')
    sidecar_options.add_argument('--memory-sidecars', action='store_true', help='Experimental eager in-memory conversion without disk cache')
    sidecar_options.add_argument('--no-sidecars', action='store_true', help='Original passthrough view (the default)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    try:
        started = time.perf_counter()
        LOG.info('Preparing virtual library')
        if args.command == 'mount' and not args.mountpoint:
            parser.error('mount requires --mountpoint')
        disk_cache = SidecarCache(args.sidecar_cache, [args.calibre, args.audio_base] +
                                  ([args.mountpoint] if args.mountpoint else [])) if args.sidecar_cache else None
        plan = build_plan(args.calibre, args.audio_base, args.audiobook_column, set(args.book_id or []))
        LOG.info('Folder planning: %.3f seconds', time.perf_counter() - started)
        view = ReadOnlyView(plan, sidecars=bool(disk_cache or args.memory_sidecars), disk_cache=disk_cache)
        plan['sidecar_summary'] = view.stats
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
