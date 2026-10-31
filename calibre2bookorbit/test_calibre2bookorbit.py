from contextlib import closing, contextmanager
import importlib.util
import errno
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import io
import json
import types
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('bookorbit', Path(__file__).with_name('calibre2bookorbit.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lib, self.audio = self.root / 'calibre', self.root / 'audio'
        self.book = self.lib / 'Author/Book (1)'
        self.book.mkdir(parents=True)
        self.audio.mkdir()
        (self.book / 'book.pdf').write_bytes(b'original PDF')
        (self.book / 'metadata.opf').write_bytes(b'original OPF')
        (self.book / 'cover.jpg').write_bytes(b'original cover')
        with self.database() as db:
            db.executescript('''CREATE TABLE books(id INTEGER,title TEXT,path TEXT);
CREATE TABLE custom_columns(id INTEGER,label TEXT);
CREATE TABLE custom_column_40(book INTEGER,value TEXT);
INSERT INTO custom_columns VALUES(40,'audiobookfiles');
INSERT INTO books VALUES(1,'Book','Author/Book (1)');
INSERT INTO custom_column_40 VALUES(1,'Recording');''')
        (self.audio / 'Recording').mkdir()

    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.lib / 'metadata.db')) as db:
            with db:
                yield db

    def tearDown(self):
        self.tmp.cleanup()

    def track(self, rel, data=b'0123456789'):
        path = self.audio / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def view(self):
        plan = m.build_plan(self.lib, self.audio)
        return plan, m.ReadOnlyView(plan)

    def read(self, view, path, size=100, offset=0):
        fh = view.open(path, os.O_RDONLY)
        try:
            return view.read(path, size, offset, fh)
        finally:
            view.release(path, fh)

    def test_flat_audio_and_opf_passthrough_without_source_changes(self):
        self.track('Recording/01.MP3')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        plan, view = self.view()
        folder = '/Author/Book (1)'
        self.assertEqual(set(view.readdir(folder)), {'.', '..', 'book.pdf', 'metadata.opf', 'cover.jpg', '01.MP3'})
        self.assertEqual(self.read(view, folder + '/metadata.opf'), b'original OPF')
        self.assertEqual(self.read(view, folder + '/01.MP3', 3, 4), b'456')
        self.assertEqual(view.getattr(folder + '/01.MP3')['st_size'], 10)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_nested_discs_flattened_in_natural_order(self):
        for disc in [10, 2, 1]:
            for track in [10, 2, 1]:
                self.track(f'Recording/Disc {disc}/{track}.mp3', f'{disc}:{track}'.encode())
        _, view = self.view()
        folder = '/Author/Book (1)'
        files = sorted(view.audio['Author/Book (1)'])
        self.assertEqual([self.read(view, folder + '/' + name).decode() for name in files],
                         [f'{d}:{t}' for d in [1, 2, 10] for t in [1, 2, 10]])
        self.assertNotIn('Disc 1', view.readdir(folder))

    def test_multiline_creates_two_complete_books(self):
        self.track('Recording/01.mp3', b'first')
        self.track('Other/01.mp3', b'second')
        with self.database() as db:
            db.execute('UPDATE custom_column_40 SET value=?', ('Recording\nOther',))
        plan, view = self.view()
        self.assertEqual(plan['virtual_books'], 2)
        for entry, expected in zip(plan['entries'], [b'first', b'second']):
            folder = '/' + entry['virtual']
            self.assertEqual(self.read(view, folder + '/book.pdf'), b'original PDF')
            self.assertEqual(self.read(view, folder + '/01.mp3'), expected)

    def test_collisions_do_not_hide_calibre_files(self):
        (self.book / '01.mp3').write_bytes(b'calibre')
        self.track('Recording/01.mp3', b'recording')
        _, view = self.view()
        folder = '/Author/Book (1)'
        self.assertEqual(self.read(view, folder + '/01.mp3'), b'calibre')
        self.assertEqual(self.read(view, folder + '/Audio 000001 - 01.mp3'), b'recording')

    def test_missing_audio_retains_ebook(self):
        (self.audio / 'Recording').rmdir()
        plan, view = self.view()
        self.assertEqual(len(plan['warnings']), 1)
        self.assertEqual(self.read(view, '/Author/Book (1)/book.pdf'), b'original PDF')

    def test_audio_only_missing_calibre_directory(self):
        self.track('Recording/01.m4b')
        for path in self.book.iterdir():
            path.unlink()
        self.book.rmdir()
        plan, view = self.view()
        self.assertEqual(self.read(view, '/Author/Book (1)/01.m4b'), b'0123456789')
        self.assertTrue(plan['warnings'])

    def test_filter_and_no_audio(self):
        self.assertEqual(m.build_plan(self.lib, self.audio, ids={9})['virtual_books'], 0)
        with self.database() as db:
            db.execute('DELETE FROM custom_column_40')
        plan, view = self.view()
        self.assertEqual(plan['audio_entries'], 0)
        self.assertIn('metadata.opf', view.readdir('/Author/Book (1)'))

    def test_audio_only_formats_excludes_foreign_metadata(self):
        for ext in ['mp3', 'm4a', 'm4b', 'opus', 'ogg', 'flac', 'jpg', 'opf', 'json']:
            self.track('Recording/file.' + ext)
        _, view = self.view()
        self.assertEqual(len(view.audio['Author/Book (1)']), 6)
        self.assertEqual(self.read(view, '/Author/Book (1)/metadata.opf'), b'original OPF')

    def test_companion_ebooks_excluded_from_audio_root_and_nested_folders(self):
        (self.book / 'book.epub').write_bytes(b'Calibre EPUB')
        self.track('Recording/01.mp3', b'audio')
        for rel in ['book.epub', 'book.pdf', 'extra.EPUB', 'Disc 1/extra.PDF',
                    'Disc 2/companion.epub', 'Disc 2/companion.pdf']:
            self.track('Recording/' + rel, b'audiobook companion ebook')
        _, view = self.view()
        folder = '/Author/Book (1)'
        self.assertEqual(set(view.readdir(folder)),
                         {'.', '..', 'book.pdf', 'book.epub', 'metadata.opf', 'cover.jpg', '01.mp3'})
        self.assertEqual(self.read(view, folder + '/book.pdf'), b'original PDF')
        self.assertEqual(self.read(view, folder + '/book.epub'), b'Calibre EPUB')
        self.assertEqual(self.read(view, folder + '/01.mp3'), b'audio')
        for rel in ['extra.EPUB', 'Disc 1/extra.PDF', 'Disc 2/companion.epub']:
            with self.assertRaises(OSError):
                view.locate(folder + '/' + rel)

    def test_write_and_traversal_denied(self):
        _, view = self.view()
        for flag in [os.O_WRONLY, os.O_RDWR, os.O_TRUNC, os.O_CREAT]:
            with self.assertRaises(OSError) as error:
                view.open('/Author/Book (1)/book.pdf', flag)
            self.assertEqual(error.exception.errno, errno.EROFS)
        with self.assertRaises(OSError):
            view.locate('/Author/Book (1)/../metadata.db')
        for rel in ['../outside', '/etc/passwd', 'C:\\outside']:
            with self.assertRaises(ValueError):
                m.contained(self.audio, rel)
        with self.assertRaises(OSError):
            view.access('/Author/Book (1)/book.pdf', os.W_OK)

    def test_linked_custom_column_schema(self):
        with self.database() as db:
            db.executescript('''DROP TABLE custom_column_40;
CREATE TABLE custom_column_40(id INTEGER,value TEXT);
CREATE TABLE books_custom_column_40_link(id INTEGER,book INTEGER,value INTEGER);
INSERT INTO custom_column_40 VALUES(7,'Recording');
INSERT INTO books_custom_column_40_link VALUES(1,1,7);''')
        self.track('Recording/01.mp3')
        plan, view = self.view()
        self.assertEqual(view.stats['audio_tracks'], 1)

    def test_preview_cli_and_source_output_guard(self):
        self.track('Recording/Disc 1/01.mp3')
        output = self.root / 'preview.json'
        args = ['calibre2bookorbit.py', 'preview', '--calibre', str(self.lib),
                '--audio-base', str(self.audio), '--output', str(output)]
        with patch.object(m.sys, 'argv', args), redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(), 0)
        report = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(report['view_summary']['audio_tracks'], 1)
        self.assertEqual(report['audio_files']['Author/Book (1)'],
                         {'Audio 000001 - 01.mp3': 'Disc 1/01.mp3'})
        args[-1] = str(self.lib / 'forbidden.json')
        with patch.object(m.sys, 'argv', args), redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(), 1)
        self.assertFalse((self.lib / 'forbidden.json').exists())

    def test_fuse_mount_wiring_is_readonly_and_rejects_occupied_target(self):
        plan, view = self.view()
        fuse = types.ModuleType('fuse')
        fuse.FUSE, fuse.Operations = Mock(), object
        fuse.FuseOSError = lambda code: OSError(code, os.strerror(code))
        target = self.root / 'mount'
        with patch.dict(m.sys.modules, {'fuse': fuse}), patch.object(m.sys, 'platform', 'linux'):
            m.mount(plan, target, True, view)
            args, flags = fuse.FUSE.call_args
            self.assertTrue(flags['ro'])
            self.assertTrue(flags['allow_other'])
            self.assertEqual(flags['fsname'], 'calibre2bookorbit')
            for operation in ['write', 'unlink', 'rename', 'create']:
                with self.assertRaises(OSError) as error:
                    getattr(args[0], operation)('unused')
                self.assertEqual(error.exception.errno, errno.EROFS)
            (target / 'occupied').write_text('keep')
            with self.assertRaises(ValueError):
                m.mount(plan, target, True, view)
            with self.assertRaises(ValueError):
                m.mount(plan, self.lib / 'mount', True, view)


if __name__ == '__main__':
    unittest.main()
