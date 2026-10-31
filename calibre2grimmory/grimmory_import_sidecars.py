#!/usr/bin/env python3
"""Import Grimmory sidecars sequentially, one committed book per request. Python 3.9+."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import time
from urllib import error, parse, request


class ImportFailure(Exception):
    pass


class AmbiguousImport(ImportFailure):
    pass


def metadata_digest(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


class LocalSidecars:
    """Read-only hints from the FUSE converter cache; ambiguous paths fall back to the API."""
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        try:
            index = json.loads((self.root / 'index.json').read_text(encoding='utf-8'))
            if not isinstance(index, dict):
                raise ValueError('invalid index')
        except (OSError, ValueError) as exc:
            raise ImportFailure('Cannot read the converter cache index: ' + str(self.root)) from exc
        self.paths = {}
        for key, record in index.items():
            if not re.fullmatch('[a-f0-9]{64}', key) or not isinstance(record, dict):
                continue
            parts = str(record.get('source', '')).replace('\\', '/').split('/')
            sha = record.get('sha256', '')
            if len(parts) < 3 or not re.fullmatch('[a-f0-9]{64}', str(sha)):
                continue
            pair = tuple(parts[-3:-1])
            self.paths.setdefault(pair, []).append((key, sha))

    def hint(self, book):
        primary = book.get('primaryFile') or {}
        parts = str(primary.get('fileSubPath') or '').replace('\\', '/').strip('/').split('/')
        found = []
        for n in range(1, len(parts)):
            leaf = re.split(r' - Audio \d{2,} - ', parts[n], maxsplit=1)[0]
            found.extend(self.paths.get((parts[n-1], leaf), []))
        return found[0] if len(found) == 1 else None

    def verify(self, hint, remote_metadata):
        key, sha = hint
        try:
            raw = (self.root / (key + '.metadata.json')).read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError('cache checksum mismatch')
            local = json.loads(raw)['metadata']
            def normalized(value):
                result = dict(value)
                for field in ('tags', 'categories', 'moods'):
                    if field in result:
                        result[field] = sorted(set(result[field]))
                return result
            if normalized(local) != normalized(remote_metadata):
                raise ValueError('cache differs from the JSON visible to Grimmory; refresh/remount the view first')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ImportFailure('Local sidecar verification failed: ' + str(exc)) from exc


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials or an import to another destination.


class Client:
    def __init__(self, url, username, password, timeout=120):
        p = parse.urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.netloc or p.username or p.password or p.query or p.fragment:
            raise ImportFailure('Use an http(s) server URL without credentials, query, or fragment.')
        self.url, self.username, self.password = url.rstrip('/'), username, password
        self.timeout, self.token = timeout, None
        self.opener = request.build_opener(NoRedirect())

    def login(self):
        result = self.call('POST', '/api/v1/auth/login',
                           {'username': self.username, 'password': self.password}, auth=False)
        self.token = result.get('accessToken') if isinstance(result, dict) else None
        if not self.token:
            raise ImportFailure('Login did not return an access token.')

    def call(self, method, path, body=None, auth=True, refreshed=False):
        headers = {'Accept': 'application/json'}
        if auth:
            if not self.token:
                self.login()
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body if body is not None else {}).encode('utf-8')
        req = request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            exc.close()
            if exc.code == 401 and auth and not refreshed:
                self.login()
                return self.call(method, path, body, auth=True, refreshed=True)
            if method == 'POST' and auth and exc.code >= 500:
                raise AmbiguousImport('Server returned HTTP %d during an import; no automatic retry. '
                                      'Check server health before resuming.' % exc.code) from exc
            raise
        except (error.URLError, TimeoutError, OSError, ValueError) as exc:
            if method == 'POST' and auth:
                raise AmbiguousImport('Import connection/response failed; the server may still be processing '
                                      'this book. No further imports started. Check the server before resuming.') from exc
            raise ImportFailure('Request failed (%s); check server availability.' % type(exc).__name__) from exc


@contextmanager
def checkpoint(path, url, library):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # OS locks are released after crashes; the small lock file can safely remain.
    lock = open(str(path) + '.lock', 'a+b')
    try:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b'0'); lock.flush(); lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ImportFailure('Another importer is using this state file.') from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ImportFailure('Another importer is using this state file.') from exc
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS target(url TEXT, library_id INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS completed(book_id INTEGER PRIMARY KEY, digest TEXT, '
                       'title TEXT, imported_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS source_tokens(book_id INTEGER PRIMARY KEY, token TEXT)')
            target = db.execute('SELECT url,library_id FROM target').fetchone()
            if target and target != (url, library['id']):
                raise ImportFailure('This state file belongs to a different server/library. '
                                    'Use a different --state file for the new library.')
            if not target:
                db.execute('INSERT INTO target VALUES(?,?)', (url, library['id']))
            db.commit()
            yield db
    finally:
        lock.close()


def duration(seconds):
    if seconds is None:
        return '--:--:--'
    seconds = max(0, int(seconds))
    return '%02d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


class Progress:
    def __init__(self, total, start, interval=5):
        self.total, self.start = total, start
        self.counts = dict(imported=0, resumed=0, unchanged=0, planned=0, missing=0, failed=0, checked=0)
        self.interval, self.last, self.width = interval, 0, 0
        self.tty = sys.stdout.isatty()

    def show(self, number, action, title='', force=False):
        now = time.monotonic()
        if not (force or self.tty or now - self.last >= self.interval):
            return
        elapsed = now - self.start
        done = sum(self.counts[k] for k in ('imported', 'resumed', 'unchanged', 'planned', 'missing', 'failed'))
        # Exclude resumed IDs from work-rate estimates.
        work = self.counts['checked']
        remaining = max(0, self.total - done)
        eta = elapsed / work * remaining if work else None
        safe_title = ''.join(c if c.isprintable() else ' ' for c in str(title))[:90]
        line = ('[%d/%d %5.1f%%] imported=%d resumed=%d unchanged=%d planned=%d missing=%d failed=%d '
                'elapsed=%s ETA=%s | %s %s') % (
                    number, self.total, 100 * done / self.total if self.total else 100,
                    self.counts['imported'], self.counts['resumed'], self.counts['unchanged'],
                    self.counts['planned'], self.counts['missing'], self.counts['failed'], duration(elapsed), duration(eta),
                    action, safe_title)
        if self.tty:
            print('\r' + line + ' ' * max(0, self.width - len(line)), end='', flush=True)
            self.width = len(line)
        else:
            print(datetime.now().astimezone().isoformat(timespec='seconds') + ' ' + line, flush=True)
        self.last = now

    def end(self):
        if self.tty:
            print(flush=True)


def run(client, library_name, state, recheck=False, dry_run=False, book_ids=None,
        limit=None, delay=0.1, max_errors=5, progress_seconds=5, sidecar_cache=None):
    started = time.monotonic()
    local = LocalSidecars(sidecar_cache) if sidecar_cache else None
    libraries = client.call('GET', '/api/v1/libraries')
    matches = [lib for lib in libraries if lib.get('name') == library_name]
    if len(matches) != 1:
        raise ImportFailure('Expected exactly one library named %r; found %d.' % (library_name, len(matches)))
    library = matches[0]
    print('Target: %s | library %s (#%s)' % (client.url, library_name, library['id']), flush=True)
    print('Loading this library\'s book list (no imports yet)...', flush=True)
    books = client.call('GET', '/api/v1/libraries/%s/book' % library['id'])
    if not isinstance(books, list):
        raise ImportFailure('Library book-list response was not a list.')
    books = sorted({int(b['id']): b for b in books}.values(), key=lambda b: b['id'])
    if book_ids:
        found = {int(b['id']) for b in books}
        missing_ids = set(book_ids) - found
        if missing_ids:
            raise ImportFailure('Requested book IDs are not in this library: ' + str(sorted(missing_ids)))
        books = [b for b in books if int(b['id']) in book_ids]
    with checkpoint(state, client.url, library) as db:
        completed = dict(db.execute('SELECT book_id,digest FROM completed'))
        tokens = dict(db.execute('SELECT book_id,token FROM source_tokens'))
        hints = {int(b['id']): local.hint(b) for b in books} if local else {}
        def can_resume(book):
            bid = int(book['id'])
            if bid not in completed:
                return False
            if local:
                return bool(hints[bid] and tokens.get(bid) == hints[bid][1])
            return not recheck
        resumed = [b for b in books if can_resume(b)]
        pending = [b for b in books if not can_resume(b)]
        if limit is not None:
            pending = pending[:limit]
        progress = Progress(len(resumed) + len(pending), started, progress_seconds)
        progress.counts['resumed'] = len(resumed)
        print('%d already completed; %d queued%s. State: %s' % (
            len(resumed), len(pending), ' (DRY RUN: no changes to Grimmory)' if dry_run else '',
            Path(state).resolve()), flush=True)
        progress.show(len(resumed), 'Starting', force=True)
        try:
            for offset, book in enumerate(pending, len(resumed) + 1):
                bid = int(book['id'])
                title = (book.get('metadata') or {}).get('title') or str(bid)
                progress.show(offset, 'Checking #%d' % bid, title)
                try:
                    sidecar = client.call('GET', '/api/v1/books/%s/sidecar' % bid)
                    if not isinstance(sidecar, dict) or not isinstance(sidecar.get('metadata'), dict) or not sidecar['metadata']:
                        raise ImportFailure('Sidecar contains no metadata object.')
                    title = sidecar['metadata'].get('title') or title
                    hint = hints.get(bid)
                    if hint:
                        local.verify(hint, sidecar['metadata'])
                    digest = metadata_digest(sidecar['metadata'])
                    if completed.get(bid) == digest:
                        progress.counts['unchanged'] += 1
                    elif dry_run:
                        progress.counts['planned'] += 1
                        progress.show(offset, 'Would import #%d' % bid, title, force=True)
                    else:
                        progress.show(offset, 'Importing #%d' % bid, title)
                        client.call('POST', '/api/v1/books/%s/sidecar/import' % bid)
                        # One local checkpoint per successful, separately committed server request.
                        db.execute('INSERT OR REPLACE INTO completed VALUES(?,?,?,?)',
                                   (bid, digest, title, datetime.now(timezone.utc).isoformat()))
                        db.commit()
                        completed[bid] = digest
                        progress.counts['imported'] += 1
                    if hint and not dry_run:
                        db.execute('INSERT OR REPLACE INTO source_tokens VALUES(?,?)', (bid, hint[1]))
                        db.commit()
                except error.HTTPError as exc:
                    if exc.code in (401, 403):
                        raise ImportFailure('Authentication/access denied (HTTP %d).' % exc.code) from exc
                    if exc.code == 404:
                        progress.counts['missing'] += 1
                    else:
                        progress.counts['failed'] += 1
                        progress.show(offset, 'FAILED #%d HTTP %d' % (bid, exc.code), title, force=True)
                except AmbiguousImport:
                    raise
                except ImportFailure as exc:
                    progress.counts['failed'] += 1
                    progress.show(offset, 'FAILED #%d: %s' % (bid, exc), title, force=True)
                progress.counts['checked'] += 1
                progress.show(offset, 'Processed #%d' % bid, title)
                if progress.counts['failed'] >= max_errors:
                    raise ImportFailure('Stopped after %d errors; successful books are checkpointed.' % max_errors)
                if delay:
                    time.sleep(delay)
            progress.show(progress.total, 'Finished', force=True)
            return 2 if progress.counts['failed'] else 0
        finally:
            progress.end()
            print('Saved successes: %s. Resume with the same command/state file.' % Path(state).resolve(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Grimmory server URL')
    parser.add_argument('--username', default='admin')
    parser.add_argument('--library', default='Books', help='Exact library name')
    parser.add_argument('--state', default='grimmory-sidecar-import.sqlite3', help='Local resume database')
    parser.add_argument('--recheck', action='store_true', help='Recheck completed books; import only changed metadata')
    parser.add_argument('--sidecar-cache', help='Local FUSE JSON cache: detect changes from its index without per-book API reads for unchanged successes')
    parser.add_argument('--dry-run', action='store_true', help='Read sidecars without importing or marking successes')
    parser.add_argument('--book-id', action='append', type=int, help='Restrict to a book ID; repeatable')
    parser.add_argument('--limit', type=int, help='Maximum pending books to process this run')
    parser.add_argument('--timeout', type=float, default=120, help='Per-request timeout in seconds')
    parser.add_argument('--delay', type=float, default=0.1, help='Pause between books in seconds')
    parser.add_argument('--max-errors', type=int, default=5)
    parser.add_argument('--progress-seconds', type=float, default=5, help='Log interval when output is redirected')
    args = parser.parse_args()
    if args.delay < 0 or args.timeout <= 0 or args.max_errors <= 0 or args.progress_seconds <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error('Timeout, limits and progress interval must be positive; delay must be nonnegative.')
    try:
        password = os.environ.get('GRIMMORY_PASSWORD') or getpass.getpass('Grimmory password: ')
        client = Client(args.url, args.username, password, args.timeout)
        return run(client, args.library, args.state, args.recheck, args.dry_run,
                   set(args.book_id or []), args.limit, args.delay, args.max_errors, args.progress_seconds,
                   args.sidecar_cache)
    except KeyboardInterrupt:
        print('\nStopped. Completed books are saved; check server health before resuming an interrupted request.', file=sys.stderr)
        return 130
    except error.HTTPError as exc:
        print('ERROR: HTTP %d while contacting Grimmory.' % exc.code, file=sys.stderr)
        return 1
    except (ImportFailure, OSError, sqlite3.Error, ValueError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
