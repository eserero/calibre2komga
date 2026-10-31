#!/usr/bin/env python3
"""Sync Calibre OPF metadata to Komga. Preview by default; --apply enables PATCH requests."""
import argparse
import base64
from collections import defaultdict
from contextlib import closing, contextmanager
import getpass
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
import re
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import time
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = load_module('_komga_sync_common', ROOT / 'calibre2grimmory/grimmory_import_sidecars.py')
calibre = load_module('_komga_sync_paths', ROOT / 'calibre_core.py')
opf = load_module('_komga_sync_opf', ROOT / 'calibre2grimmory/calibre2grimmory.py')
VERSION = 1


class Client:
    def __init__(self, url, username, password, timeout=120):
        p = parse.urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.netloc or p.username or p.password or p.query or p.fragment:
            raise common.ImportFailure('Use an http(s) URL without credentials, query or fragment.')
        self.url, self.timeout = url.rstrip('/'), timeout
        self.authorization = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
        self.opener = request.build_opener(common.NoRedirect())

    def call(self, method, path, body=None):
        if method not in ('GET', 'PATCH'):
            raise common.ImportFailure('Only GET and individual metadata PATCH requests are supported.')
        headers = {'Authorization': self.authorization, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        try:
            with self.opener.open(request.Request(self.url + path, data=data, headers=headers, method=method),
                                  timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            exc.close()
            if method == 'PATCH' and exc.code >= 500:
                raise common.AmbiguousImport('HTTP %d during PATCH; stopped without retry. Check server state before resuming.' % exc.code) from exc
            raise
        except (error.URLError, TimeoutError, OSError, ValueError) as exc:
            if method == 'PATCH':
                raise common.AmbiguousImport('PATCH outcome is uncertain; no further updates started. Check Komga before resuming.') from exc
            raise common.ImportFailure('Komga request failed (%s).' % type(exc).__name__) from exc


def pages(client, resource, library_id):
    result = []
    page = 0
    while True:
        data = client.call('GET', '/api/v1/%s?%s' % (resource, parse.urlencode(
            {'library_id': library_id, 'page': page, 'size': 500})))
        if not isinstance(data, dict) or not isinstance(data.get('content'), list):
            raise common.ImportFailure('Unexpected paginated %s response.' % resource)
        content = data['content']
        if any(x.get('libraryId') is not None and str(x['libraryId']) != str(library_id) for x in content):
            raise common.ImportFailure('Server returned items from another library; refusing to continue.')
        result.extend(x for x in content if not x.get('deleted'))
        if data.get('last') is True or not content or page + 1 >= data.get('totalPages', page + 2):
            return result
        page += 1


def remote_path(value):
    value = str(value or '').replace('\\', '/')
    p = parse.urlsplit(value)
    if p.scheme == 'file':
        value = parse.unquote(p.path)
    elif p.scheme and len(p.scheme) != 1:
        raise ValueError('Not a filesystem path: ' + value)
    path = PurePosixPath(value)
    if '..' in path.parts:
        raise ValueError('Unsafe path: ' + value)
    return path


def relative_remote(value, root):
    path = remote_path(value).relative_to(remote_path(root)).as_posix()
    # The working mount preserves filename case but may expose upper-case format suffixes.
    p = PurePosixPath(path)
    return str(p.with_suffix(p.suffix.lower()))


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'blockquote'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'blockquote'):
            self.parts.append('\n')


def summary(value):
    p = Text()
    p.feed(value)
    return '\n'.join(line for line in (' '.join(s.split()) for s in ''.join(p.parts).splitlines()) if line)


def book_payload(metadata):
    result = {}
    for source, dest in [('title', 'title'), ('publishedDate', 'releaseDate')]:
        if metadata.get(source):
            result[dest] = metadata[source]
    if metadata.get('description'):
        result['summary'] = summary(metadata['description'])
    if metadata.get('authors'):
        result['authors'] = [{'name': name, 'role': 'writer'} for name in metadata['authors']]
    if metadata.get('tags'):
        result['tags'] = sorted(set(metadata['tags']))
    isbn = metadata.get('isbn13') or metadata.get('isbn10')
    if isbn:
        result['isbn'] = isbn
    series = metadata.get('series') or {}
    if series.get('name') and series.get('number') is not None:
        result['number'] = '%g' % series['number']
        result['numberSort'] = series['number']
    return result


def equivalent(field, a, b):
    if field in ('tags', 'genres'):
        return set(a or []) == set(b or [])
    if field == 'authors':
        return sorted((x['name'], x['role']) for x in a or []) == sorted((x['name'], x['role']) for x in b or [])
    return a == b


@contextmanager
def state_file(path, url, library_id, library_root):
    path = Path(path).resolve()
    library_root = Path(library_root).resolve()
    if path == library_root or library_root in path.parents:
        raise common.ImportFailure('State/cache database must be outside the Calibre source library.')
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b'0'); lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise common.ImportFailure('Another sync is using this state file.') from exc
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS destination(url TEXT, library_id TEXT, source TEXT)')
            target = db.execute('SELECT url,library_id,source FROM destination').fetchone()
            expected = (url, str(library_id), str(library_root))
            if target and target != expected:
                raise common.ImportFailure('State belongs to a different server/library/Calibre root. Use a new --state file.')
            if not target:
                db.execute('INSERT INTO destination VALUES(?,?,?)', expected)
            db.execute('CREATE TABLE IF NOT EXISTS source_cache(path TEXT PRIMARY KEY, signature TEXT, metadata TEXT, '
                       'directory_stamp TEXT, filenames TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS synced(kind TEXT, id TEXT, digest TEXT, PRIMARY KEY(kind,id))')
            db.commit()
            yield db


def source_map(library, db):
    library = Path(library).resolve()
    store = calibre.CalibreMetadataStore(library)
    with closing(sqlite3.connect((library / 'metadata.db').as_uri() + '?mode=ro', uri=True)) as source:
        source.execute('BEGIN')
        rows = source.execute('''SELECT b.id,b.title,b.path,b.series_index,a.name,s.name,GROUP_CONCAT(d.name,',')
            FROM books b LEFT JOIN books_authors_link bal ON b.id=bal.book
            LEFT JOIN authors a ON bal.author=a.id LEFT JOIN books_series_link l ON b.id=l.book
            LEFT JOIN series s ON l.series=s.id LEFT JOIN data d ON b.id=d.book
            GROUP BY b.id ORDER BY a.name,s.name,b.series_index,b.title''').fetchall()
    mapping = defaultdict(list)
    hits = converted = errors = 0
    for count, (bid, title, path, number, author, series, _formats) in enumerate(rows, 1):
        try:
            folder = opf.contained(library, path)
            metadata_path = opf.contained(library, str(PurePosixPath(path) / 'metadata.opf'))
            st = metadata_path.stat()
            stamp = json.dumps([VERSION, opf.CACHE_VERSION, st.st_mtime_ns, st.st_ctime_ns, st.st_size])
            cached = db.execute('SELECT signature,metadata,directory_stamp,filenames FROM source_cache WHERE path=?', (path,)).fetchone()
            if cached and cached[0] == stamp:
                metadata = json.loads(cached[1]); hits += 1
            else:
                metadata = opf.opf_metadata(metadata_path); converted += 1
                after = metadata_path.stat()
                if (after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (st.st_mtime_ns, st.st_ctime_ns, st.st_size):
                    raise ValueError('OPF changed during parsing; retry after Calibre saves it')
            ds = folder.stat()
            directory_stamp = str((ds.st_mtime_ns, ds.st_ctime_ns))
            if cached and cached[2] == directory_stamp:
                filenames = json.loads(cached[3])
            else:
                filenames = [p.name for p in store.find_ebook_files(folder)]
            if not cached or cached[0] != stamp or cached[2] != directory_stamp:
                db.execute('INSERT OR REPLACE INTO source_cache VALUES(?,?,?,?,?)',
                           (path, stamp, json.dumps(metadata), directory_stamp, json.dumps(filenames)))
            # Deliberately use the existing mount's naming functions without modifying them.
            mount_metadata = dict(title=title, series=series, series_index=number,
                                  author=(author.split(',')[0].strip() if author else 'Unknown Author'))
            record = dict(calibre_id=bid, metadata=metadata, source=path)
            for filename in filenames:
                rel = PurePosixPath(*store.get_virtual_segments(mount_metadata, filename))
                rel = str(rel.with_suffix(rel.suffix.lower()))
                if record not in mapping[rel]:
                    mapping[rel].append(record)
        except (OSError, ValueError, opf.ET.ParseError) as exc:
            errors += 1
            print('WARNING: Calibre #%s: %s' % (bid, exc), flush=True)
        if count % 500 == 0:
            db.commit()
            print('Checking Calibre: %d/%d; OPFs reused=%d parsed=%d errors=%d' %
                  (count, len(rows), hits, converted, errors), flush=True)
    db.commit()
    print('Calibre ready: OPFs reused=%d parsed=%d errors=%d' % (hits, converted, errors), flush=True)
    return mapping


def parse_file_types(values):
    if values is None:
        return None
    result = set()
    for value in values:
        for token in value.split(','):
            token = token.strip().lower().lstrip('.')
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', token):
                raise ValueError('File types must be comma-separated extensions, for example pdf,epub.')
            result.add(token)
    if not result:
        raise ValueError('Specify at least one file type, or omit --file-types for all types.')
    return result


def selected_book(book, file_types):
    return file_types is None or remote_path(book.get('url')).suffix.lower().lstrip('.') in file_types


def plan_sync(books, series, mapping, root, file_types=None):
    operations, warnings, matches, groups = [], [], {}, defaultdict(list)
    urls = defaultdict(list)
    for book in books:
        try:
            if not selected_book(book, file_types):
                continue
            rel = relative_remote(book.get('url'), root)
            urls[rel].append(book)
        except ValueError:
            warnings.append('Book #%s is outside the library root or has no visible path.' % book['id'])
    for rel, remote_books in urls.items():
        candidates = mapping.get(rel, [])
        if len(candidates) != 1 or len(remote_books) != 1:
            warnings.append('Unmatched/ambiguous path skipped: ' + rel)
            continue
        book, record = remote_books[0], candidates[0]
        matches[str(book['id'])] = record
        operations.append(dict(kind='books', id=str(book['id']), title=record['metadata'].get('title', rel),
                               desired=book_payload(record['metadata']), current=book.get('metadata') or {},
                               path=rel, calibre_id=record['calibre_id']))
    for book in books:
        groups[str(book['seriesId'])].append(book)
    series_by_id = {str(s['id']): s for s in series}
    for sid, members in groups.items():
        selected_ids = {str(b['id']) for b in members if selected_book(b, file_types)}
        if not selected_ids:
            continue
        if len(selected_ids) != len(members):
            warnings.append('Series #%s descriptive metadata skipped: contains books outside the file-type filter.' % sid)
            continue
        if any(str(b['id']) not in matches for b in members) or sid not in series_by_id:
            warnings.append('Series #%s metadata skipped: not every member has an unambiguous Calibre match.' % sid)
            continue
        values = [matches[str(b['id'])]['metadata'] for b in members]
        desired = {}
        for field in ('publisher', 'language'):
            consensus = {m.get(field) for m in values}
            if len(consensus) == 1 and next(iter(consensus)):
                desired[field] = next(iter(consensus))
            elif len(consensus) > 1:
                warnings.append('Series #%s: conflicting/missing %s values; field skipped.' % (sid, field))
        genres = sorted({x for m in values for x in m.get('categories', [])})
        if genres:
            desired['genres'] = genres
        if desired:
            remote = series_by_id[sid]
            operations.append(dict(kind='series', id=sid, title=(remote.get('metadata') or {}).get('title', sid),
                                   desired=desired, current=remote.get('metadata') or {}))
    return operations, warnings


def run(client, library_name, calibre_root, state, apply=False, force=False,
        overwrite_locked=False, limit=None, delay=0.1, report=None, file_types=None):
    started = time.monotonic()
    if report:
        dest = Path(report).resolve()
        root = Path(calibre_root).resolve()
        if dest == root or root in dest.parents:
            raise common.ImportFailure('Report must be outside the Calibre source.')
    libs = [x for x in client.call('GET', '/api/v1/libraries') if x.get('name') == library_name]
    if len(libs) != 1:
        raise common.ImportFailure('Expected exactly one library named %r.' % library_name)
    lib = libs[0]
    if not lib.get('root'):
        raise common.ImportFailure('Library path is hidden. Sign in as a Komga administrator.')
    print('Target: %s | %s (#%s) root=%s | %s' %
          (client.url, library_name, lib['id'], lib['root'], 'APPLY' if apply else 'PREVIEW (no server writes)'), flush=True)
    with state_file(state, client.url, lib['id'], calibre_root) as db:
        mapping = source_map(calibre_root, db)
        print('Reading Komga book/series indexes (paged; no per-book GET requests)...', flush=True)
        books = pages(client, 'books', lib['id'])
        series = pages(client, 'series', lib['id'])
        selected_count = sum(selected_book(b, file_types) for b in books)
        print('File types: %s | %d of %d books selected' %
              (', '.join(sorted(file_types)) if file_types is not None else 'all', selected_count, len(books)), flush=True)
        operations, warnings = plan_sync(books, series, mapping, lib['root'], file_types)
        if selected_count and not any(x['kind'] == 'books' for x in operations):
            raise common.ImportFailure('No unambiguous book paths matched. Check the selected library and working Komga mount; no updates applied.')
        for warning in warnings:
            print('WARNING: ' + warning, flush=True)
        print('%d book/series operations matched; %d matching/consensus warnings.' %
              (len(operations), len(warnings)), flush=True)
        progress = common.Progress(len(operations), started)
        report_rows, sent = [], 0
        try:
            for index, operation in enumerate(operations, 1):
                kind, oid = operation['kind'], operation['id']
                desired, current = operation['desired'], operation['current']
                digest = common.metadata_digest({'version': VERSION, 'desired': desired,
                                                 'overwrite_locked': overwrite_locked})
                saved = db.execute('SELECT digest FROM synced WHERE kind=? AND id=?', (kind, oid)).fetchone()
                if saved and saved[0] == digest and not force:
                    progress.counts['resumed'] += 1
                else:
                    locked = [f for f in desired if current.get(f + 'Lock') and not overwrite_locked]
                    delta = {f: v for f, v in desired.items() if f not in locked and not equivalent(f, v, current.get(f))}
                    if locked:
                        print('\nWARNING: %s #%s locked fields skipped: %s' % (kind, oid, ', '.join(locked)), flush=True)
                    if delta:
                        report_rows.append(dict(kind=kind, id=oid, title=operation['title'], patch=delta))
                        progress.show(index, ('Updating ' if apply else 'Would update ') + kind + ' #' + oid,
                                      operation['title'], force=not apply)
                        if apply:
                            client.call('PATCH', '/api/v1/%s/%s/metadata' % (kind, parse.quote(oid, safe='')), delta)
                            progress.counts['imported'] += 1
                        else:
                            progress.counts['planned'] += 1
                        sent += 1
                    else:
                        progress.counts['unchanged'] += 1
                    if apply:
                        db.execute('INSERT OR REPLACE INTO synced VALUES(?,?,?)', (kind, oid, digest))
                        db.commit()
                progress.counts['checked'] += 1
                progress.show(index, 'Processed ' + kind + ' #' + oid, operation['title'])
                if limit is not None and sent >= limit:
                    progress.show(index, 'Update limit reached', force=True)
                    break
                if delay and apply:
                    time.sleep(delay)
            else:
                progress.show(len(operations), 'Finished', force=True)
        finally:
            progress.end()
        if report:
            dest.write_text(json.dumps(dict(library=library_name, updates=report_rows, warnings=warnings),
                                       ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Only supported, supplied fields are managed; folders and book membership were not changed.', flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--username', required=True, help='Komga administrator email/login')
    parser.add_argument('--library', required=True, help='Exact Komga library name')
    parser.add_argument('--calibre', required=True, help='Calibre library containing metadata.db and OPFs')
    parser.add_argument('--state', default='komga-metadata-sync.sqlite3')
    parser.add_argument('--apply', action='store_true', help='Apply previewed metadata changes')
    parser.add_argument('--force', action='store_true', help='Recompare checkpointed fields with current server metadata')
    parser.add_argument('--file-types', action='append', metavar='EXT[,EXT...]',
                        help='Only update these filename extensions, e.g. pdf or pdf,epub; repeatable; default all')
    parser.add_argument('--overwrite-locked', action='store_true', help='Allow changing values of locked metadata fields')
    parser.add_argument('--limit', type=int, help='Maximum book/series PATCHes (or proposed PATCHes)')
    parser.add_argument('--delay', type=float, default=0.1)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--report', help='Save proposed/applied PATCH payloads to JSON')
    args = parser.parse_args()
    if args.delay < 0 or args.timeout <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error('Timeout and limit must be positive; delay must be nonnegative.')
    try:
        file_types = parse_file_types(args.file_types)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        password = os.environ.get('KOMGA_PASSWORD') or getpass.getpass('Komga password: ')
        return run(Client(args.url, args.username, password, args.timeout), args.library,
                   args.calibre, args.state, args.apply, args.force,
                   args.overwrite_locked, args.limit, args.delay, args.report, file_types)
    except KeyboardInterrupt:
        print('\nStopped. Completed PATCHes are checkpointed; check an interrupted request before resuming.', file=sys.stderr)
        return 130
    except error.HTTPError as exc:
        print('ERROR: Komga HTTP %d. Successful earlier updates are checkpointed.' % exc.code, file=sys.stderr)
        return 1
    except (common.ImportFailure, OSError, sqlite3.Error, ValueError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
