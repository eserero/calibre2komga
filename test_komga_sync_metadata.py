from contextlib import closing, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib import parse

spec = importlib.util.spec_from_file_location('komga_sync', Path(__file__).with_name('komga_sync_metadata.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeKomga:
    url = 'http://fixture.invalid'

    def __init__(self):
        self.library = {'id': 'lib1', 'name': 'Books', 'root': '/books'}
        self.books = [dict(id='b1', libraryId='lib1', seriesId='s1', url='/books/Series/Volume 01 - First.epub', metadata={'title': 'Old'}),
                      dict(id='b2', libraryId='lib1', seriesId='s1', url='/books/Series/Volume 02 - Second.pdf', metadata={'title': 'Old'}),
                      dict(id='b3', libraryId='lib1', seriesId='s2', url='/books/Writer/_oneshot/Single.epub', metadata={'title': 'Old'})]
        self.series = [dict(id='s1', metadata={'title': 'Folder title'}), dict(id='s2', metadata={'title': 'Writer'})]
        self.calls, self.fail = [], None

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'PATCH':
            if self.fail and self.fail in path:
                raise m.common.AmbiguousImport('uncertain request')
            parts = path.split('/')
            target = self.books if parts[3] == 'books' else self.series
            next(x for x in target if x['id'] == parts[4])['metadata'].update(body)
            return {}
        if path == '/api/v1/libraries':
            return [self.library]
        parsed = parse.urlsplit(path)
        page = int(parse.parse_qs(parsed.query)['page'][0])
        data = self.books if parsed.path == '/api/v1/books' else self.series
        return {'content': data[page*2:page*2+2], 'totalPages': (len(data)+1)//2,
                'last': (page+1)*2 >= len(data)}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lib = self.root / 'calibre'; self.lib.mkdir()
        self.state = self.root / 'sync.sqlite3'
        with closing(sqlite3.connect(self.lib / 'metadata.db')) as db:
            db.executescript('''CREATE TABLE books(id INTEGER,title TEXT,path TEXT,series_index REAL);
                CREATE TABLE authors(id INTEGER,name TEXT); CREATE TABLE books_authors_link(book INTEGER,author INTEGER);
                CREATE TABLE series(id INTEGER,name TEXT); CREATE TABLE books_series_link(book INTEGER,series INTEGER);
                CREATE TABLE data(book INTEGER,name TEXT,format TEXT);
                INSERT INTO authors VALUES(1,'Writer'); INSERT INTO series VALUES(1,'Series');
                INSERT INTO books VALUES(1,'First','Writer/First (1)',1),(2,'Second','Writer/Second (2)',2),(3,'Single','Writer/Single (3)',1);
                INSERT INTO books_authors_link VALUES(1,1),(2,1),(3,1);
                INSERT INTO books_series_link VALUES(1,1),(2,1);
                INSERT INTO data VALUES(1,'First','EPUB'),(2,'Second','PDF'),(3,'Single','EPUB');''')
            db.commit()
        self.files = []
        for i, title, ext in [(1, 'First', 'epub'), (2, 'Second', 'pdf'), (3, 'Single', 'epub')]:
            folder = self.lib / ('Writer/%s (%s)' % (title, i)); folder.mkdir(parents=True)
            (folder / (title + '.' + ext)).write_bytes(b'original media')
            xml = '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
            <dc:title>%s</dc:title><dc:creator>Writer</dc:creator><dc:publisher>Publisher</dc:publisher><dc:language>eng</dc:language>
            <dc:description>&lt;p&gt;Description&lt;/p&gt;</dc:description><dc:subject>Fantasy</dc:subject>%s</metadata></package>''' % (
                title, ('<meta name="calibre:series" content="Series"/><meta name="calibre:series_index" content="%s"/>' % i) if i < 3 else '')
            p = folder / 'metadata.opf'; p.write_text(xml, encoding='utf-8'); self.files.append(p)
        self.client = FakeKomga()

    def tearDown(self):
        self.temp.cleanup()

    def run_sync(self, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            result = m.run(self.client, 'Books', self.lib, self.state, delay=0, **kwargs)
        return result, output.getvalue()

    def patches(self):
        return [(path, body) for method, path, body in self.client.calls if method == 'PATCH']

    def test_preview_and_read_only_sources(self):
        before = {p:p.read_bytes() for p in self.lib.rglob('*') if p.is_file()}
        result, _ = self.run_sync(report=self.root/'preview.json')
        self.assertEqual(result, 0); self.assertEqual(self.patches(), [])
        self.assertEqual(len(json.loads((self.root/'preview.json').read_text())['updates']), 5)
        self.assertEqual(before, {p:p.read_bytes() for p in self.lib.rglob('*') if p.is_file()})

    def test_apply_then_unchanged_run_sends_no_patches_or_opf_parses(self):
        self.run_sync(apply=True); self.assertEqual(len(self.patches()), 5)
        self.assertEqual(self.client.books[0]['metadata']['numberSort'], 1)
        self.assertEqual(self.client.books[0]['metadata']['summary'], 'Description')
        self.assertEqual(self.client.series[0]['metadata']['title'], 'Folder title')
        self.client.calls.clear()
        with patch.object(m.opf, 'opf_metadata', side_effect=AssertionError('Unchanged OPF parsed')):
            self.run_sync(apply=True)
        self.assertEqual(self.patches(), [])
        self.assertTrue(all('?' in p or p.endswith('/libraries') for method,p,body in self.client.calls))

    def test_one_opf_change_updates_only_one_book(self):
        self.run_sync(apply=True); self.client.calls.clear()
        p = self.files[1]; p.write_text(p.read_text().replace('Description', 'New description'), encoding='utf-8')
        self.run_sync(apply=True)
        self.assertEqual(self.patches(), [('/api/v1/books/b2/metadata', {'summary':'New description'})])

    def test_uncertain_patch_preserves_completed_successes(self):
        self.client.fail = '/books/b2/'
        with self.assertRaises(m.common.AmbiguousImport):
            self.run_sync(apply=True)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute('SELECT kind,id FROM synced').fetchall(), [('books','b1')])
        self.client.fail = None; self.client.calls.clear(); self.run_sync(apply=True)
        self.assertNotIn('/api/v1/books/b1/metadata', [p for p,b in self.patches()])

    def test_locks_and_force_revalidation(self):
        self.client.books[0]['metadata']['titleLock'] = True
        self.run_sync(apply=True)
        self.assertEqual(self.client.books[0]['metadata']['title'], 'Old')
        self.client.calls.clear(); self.run_sync(apply=True, overwrite_locked=True)
        self.assertEqual(self.client.books[0]['metadata']['title'], 'First')
        self.client.books[1]['metadata']['title'] = 'Remote drift'; self.client.calls.clear()
        self.run_sync(apply=True, overwrite_locked=True)
        self.assertEqual(self.patches(), [])
        self.run_sync(apply=True, overwrite_locked=True, force=True)
        self.assertEqual(self.client.books[1]['metadata']['title'], 'Second')

    def test_ambiguous_matching_and_series_consensus(self):
        with m.state_file(self.state, self.client.url, 'lib1', self.lib) as db:
            mapping = m.source_map(self.lib, db)
        mapping['Series/Volume 01 - First.epub'].append({'calibre_id':999,'metadata':{},'source':'other'})
        operations, warnings = m.plan_sync(self.client.books, self.client.series, mapping, '/books')
        self.assertFalse(any(x['id']=='b1' for x in operations))
        self.assertFalse(any(x['kind']=='series' and x['id']=='s1' for x in operations))
        self.assertTrue(warnings)
        mapping['Series/Volume 01 - First.epub'].pop()
        mapping['Series/Volume 02 - Second.pdf'][0]['metadata']['publisher'] = 'Another publisher'
        operations, _ = m.plan_sync(self.client.books, self.client.series, mapping, '/books')
        series = next(x for x in operations if x['kind']=='series' and x['id']=='s1')
        self.assertNotIn('title', series['desired'])
        self.assertNotIn('publisher', series['desired'])

    def test_path_scope_and_target_identity(self):
        self.assertEqual(m.relative_remote('file:///books/Series/Volume%2001%20-%20First.EPUB', '/books'),
                         'Series/Volume 01 - First.epub')
        with self.assertRaises(ValueError):m.relative_remote('/other/Book.epub', '/books')
        self.run_sync()
        self.client.library['id'] = 'different'
        with self.assertRaises(m.common.ImportFailure):self.run_sync(apply=True)
        self.assertEqual(self.patches(), [])

    def test_limit_and_no_match_failure(self):
        self.run_sync(apply=True, limit=1); self.assertEqual(len(self.patches()),1)
        self.client.calls.clear()
        for b in self.client.books:b['url'] = '/other/unmatched.epub'
        with self.assertRaises(m.common.ImportFailure):self.run_sync(apply=True)
        self.assertEqual(self.patches(), [])

    def test_library_scope_and_report_guard(self):
        self.client.books[0]['libraryId']='other-library'
        with self.assertRaises(m.common.ImportFailure):self.run_sync(apply=True)
        self.assertEqual(self.patches(),[])
        self.client.books[0]['libraryId']='lib1'
        self.client.calls.clear()
        with self.assertRaises(m.common.ImportFailure):self.run_sync(apply=True,report=self.lib/'report.json')
        self.assertEqual(self.client.calls,[])

    def test_real_http_basic_auth_and_individual_patches(self):
        fixture=self.client
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                raw=json.dumps(fixture.call('GET',self.path)).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_PATCH(self):
                seen.append(self.headers.get('Authorization'))
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.call('PATCH',self.path,body)
                self.send_response(204);self.end_headers()
        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                self.client=m.Client('http://127.0.0.1:%d'%server.server_port,'test','secret')
                self.run_sync(apply=True)
                self.assertEqual(len([x for x in fixture.calls if x[0]=='PATCH']),5)
                self.assertTrue(all(value=='Basic dGVzdDpzZWNyZXQ=' for value in seen))
            finally:
                server.shutdown();worker.join()

    def test_pdf_filter_does_not_modify_epubs_or_mixed_series(self):
        _, output = self.run_sync(apply=True, file_types={'pdf'})
        self.assertIn('1 of 3 books selected', output)
        self.assertEqual([path for path,body in self.patches()], ['/api/v1/books/b2/metadata'])
        self.assertEqual(self.client.books[0]['metadata'], {'title':'Old'})
        self.assertEqual(self.client.series[0]['metadata'], {'title':'Folder title'})
        self.client.calls.clear()
        self.run_sync(apply=True, file_types={'pdf'})
        self.assertEqual(self.patches(), [])
        self.run_sync(apply=True)
        self.assertNotIn('/api/v1/books/b2/metadata', [path for path,body in self.patches()])
        self.assertEqual(self.client.books[0]['metadata']['title'], 'First')

    def test_multiple_extensions_and_no_matching_type(self):
        self.assertEqual(m.parse_file_types(['.PDF,EPUB','cbz']), {'pdf','epub','cbz'})
        self.assertIsNone(m.parse_file_types(None))
        for invalid in [['pdf,,epub'],['../pdf'],['']]:
            with self.assertRaises(ValueError):m.parse_file_types(invalid)
        _, output = self.run_sync(apply=True, file_types={'cbr'})
        self.assertIn('0 of 3 books selected', output)
        self.assertEqual(self.patches(), [])
        self.run_sync(apply=True, file_types={'epub','pdf'})
        self.assertEqual(len(self.patches()),5)

    def test_pdf_only_series_can_receive_descriptive_fields_but_no_rename(self):
        (self.lib/'Writer/First (1)/First.pdf').write_bytes(b'PDF')
        self.client.books[0]['url']='/books/Series/Volume 01 - First.pdf'
        self.run_sync(apply=True, file_types={'pdf'})
        self.assertEqual(len(self.patches()),3)
        series_patch=next(body for path,body in self.patches() if path=='/api/v1/series/s1/metadata')
        self.assertEqual(series_patch,{'publisher':'Publisher','language':'en'})
        self.assertNotIn('title',series_patch)


if __name__ == '__main__':
    unittest.main(verbosity=2)
