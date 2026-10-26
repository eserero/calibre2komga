import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('importer', Path(__file__).with_name('grimmory_import_sidecars.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeClient:
    url = 'http://fixture.invalid'

    def __init__(self):
        self.library_id = 11
        self.books = [{'id': i, 'metadata': {'title': 'Book %s' % i}} for i in [1, 2, 3]]
        self.sidecars = {1: {'metadata': {'title': 'First', 'series': {'name': 'Series', 'number': 1}}},
                         2: {'metadata': {'title': 'Second'}}}
        self.calls = []
        self.fail_on = {}

    def call(self, method, path):
        self.calls.append((method, path))
        if (method, path) in self.fail_on:
            raise self.fail_on[(method, path)]
        if path == '/api/v1/libraries':
            return [{'id': self.library_id, 'name': 'Books'}]
        if path == '/api/v1/libraries/%s/book' % self.library_id:
            return self.books
        bid = int(path.split('/')[4])
        if method == 'POST':
            return {'message': 'Sidecar metadata imported successfully'}
        if bid not in self.sidecars:
            raise error.HTTPError(self.url + path, 404, 'Missing', {}, None)
        return self.sidecars[bid]


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'resume.sqlite3'
        self.client = FakeClient()

    def tearDown(self):
        self.temp.cleanup()

    def run_import(self, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            result = m.run(self.client, 'Books', self.state, delay=0, **kwargs)
        return result, output.getvalue()

    def posts(self):
        return [path for method, path in self.client.calls if method == 'POST']

    def completed(self):
        with closing(sqlite3.connect(self.state)) as db:
            return [r[0] for r in db.execute('SELECT book_id FROM completed ORDER BY book_id')]

    def test_sequential_import_and_resume(self):
        result, output = self.run_import()
        self.assertEqual(result, 0)
        self.assertEqual(self.posts(), ['/api/v1/books/1/sidecar/import', '/api/v1/books/2/sidecar/import'])
        self.assertEqual(self.completed(), [1, 2])
        self.assertIn('imported=2', output)
        self.assertIn('missing=1', output)
        self.assertIn('ETA=', output)
        self.client.calls.clear()
        _, output = self.run_import()
        self.assertEqual(self.posts(), [])
        self.assertNotIn(('GET', '/api/v1/books/1/sidecar'), self.client.calls)
        self.assertIn('resumed=2', output)

    def test_recheck_imports_only_changed_metadata(self):
        self.run_import()
        self.client.sidecars[1]['generatedAt'] = 'changed generation time only'
        self.client.sidecars[2]['metadata']['publisher'] = 'New Publisher'
        self.client.calls.clear()
        _, output = self.run_import(recheck=True)
        self.assertEqual(self.posts(), ['/api/v1/books/2/sidecar/import'])
        self.assertIn('unchanged=1', output)

    def test_error_does_not_lose_preceding_success(self):
        self.client.fail_on[('POST', '/api/v1/books/2/sidecar/import')] = m.AmbiguousImport('timeout')
        with self.assertRaises(m.AmbiguousImport):
            self.run_import()
        self.assertEqual(self.completed(), [1])
        self.assertNotIn(('GET', '/api/v1/books/3/sidecar'), self.client.calls)
        self.client.fail_on.clear()
        self.client.calls.clear()
        self.run_import()
        self.assertEqual(self.posts(), ['/api/v1/books/2/sidecar/import'])

    def test_wrong_library_checkpoint_is_rejected(self):
        self.run_import()
        self.client.library_id = 12
        self.client.calls.clear()
        with self.assertRaises(m.ImportFailure):
            self.run_import()
        self.assertEqual(self.posts(), [])

    def test_dry_run_does_not_import_or_checkpoint_books(self):
        result, output = self.run_import(dry_run=True)
        self.assertEqual(result, 0)
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.completed(), [])
        self.assertIn('planned=2', output)

    def test_limit_and_book_scope(self):
        self.run_import(limit=1)
        self.assertEqual(self.completed(), [1])
        self.client.calls.clear()
        self.run_import(limit=1)
        self.assertEqual(self.posts(), ['/api/v1/books/2/sidecar/import'])
        with self.assertRaises(m.ImportFailure):
            self.run_import(book_ids={999})

    def test_invalid_sidecar_and_error_limit(self):
        self.client.sidecars[1] = {'metadata': {}}
        self.client.sidecars[2] = []
        with self.assertRaises(m.ImportFailure):
            self.run_import(max_errors=2)
        self.assertEqual(self.posts(), [])

    def test_exclusive_state_lock(self):
        with m.checkpoint(self.state, self.client.url, {'id': 11}):
            with self.assertRaises(m.ImportFailure):
                with m.checkpoint(self.state, self.client.url, {'id': 11}):
                    pass

    def test_token_refresh_and_no_ambiguous_retry(self):
        client = m.Client('http://fixture.invalid', 'admin', 'secret')
        client.token = 'old'

        class Reply(io.BytesIO):
            pass

        replies = [error.HTTPError('http://fixture.invalid', 401, 'Unauthorized', {}, None),
                   Reply(b'{"accessToken":"new"}'), Reply(b'{"message":"OK"}')]
        with patch.object(client.opener, 'open', side_effect=replies) as opened:
            self.assertEqual(client.call('POST', '/api/v1/books/1/sidecar/import')['message'], 'OK')
            self.assertEqual(opened.call_count, 3)
        for failure in [TimeoutError(), error.HTTPError('http://fixture.invalid', 500, 'Error', {}, None)]:
            with patch.object(client.opener, 'open', side_effect=failure) as opened:
                with self.assertRaises(m.AmbiguousImport):
                    client.call('POST', '/api/v1/books/1/sidecar/import')
                self.assertEqual(opened.call_count, 1)

    def test_actual_http_client_uses_individual_endpoints(self):
        fixture = self.client
        seen_headers = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen_headers.append(self.headers.get('Authorization'))
                try:
                    self.reply(200, fixture.call('GET', self.path))
                except error.HTTPError as exc:
                    self.reply(exc.code, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/api/v1/auth/login':
                    self.reply(200, {'accessToken': 'fixture-token'})
                else:
                    seen_headers.append(self.headers.get('Authorization'))
                    self.reply(200, fixture.call('POST', self.path))

            def reply(self, status, body):
                data = json.dumps(body).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                self.client = m.Client('http://127.0.0.1:%s' % server.server_port, 'fixture-user', 'fixture-password')
                result, _ = self.run_import()
                self.assertEqual(result, 0)
                self.assertEqual(self.completed(), [1, 2])
                self.assertEqual([p for method, p in fixture.calls if method == 'POST'],
                                 ['/api/v1/books/1/sidecar/import', '/api/v1/books/2/sidecar/import'])
                self.assertTrue(all(h == 'Bearer fixture-token' for h in seen_headers))
            finally:
                server.shutdown()
                worker.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
