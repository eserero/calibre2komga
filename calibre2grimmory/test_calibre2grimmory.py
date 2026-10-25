import importlib.util,os,sqlite3,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('c2g',Path(__file__).with_name('calibre2grimmory.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.lib=self.root/'calibre';self.audio=self.root/'audio';self.lib.mkdir();self.audio.mkdir()
  for name in ['Author/Book (1)','Author/Only audio (2)','Author/No audio (3)']:(self.lib/name).mkdir(parents=True)
  (self.lib/'Author/Book (1)/book.epub').write_bytes(b'unchanged EPUB bytes')
  (self.lib/'Author/Book (1)/metadata.opf').write_text('original metadata')
  (self.lib/'Author/No audio (3)/book.pdf').write_bytes(b'PDF')
  for name in ['Recording A/disc 1','Recording B']:(self.audio/name).mkdir(parents=True)
  (self.audio/'Recording A/disc 1/01.mp3').write_bytes(b'0123456789')
  (self.audio/'Recording B/01.m4b').write_bytes(b'audio B')
  c=sqlite3.connect(self.lib/'metadata.db');c.executescript('''CREATE TABLE books(id INTEGER,title TEXT,path TEXT);
CREATE TABLE custom_columns(id INTEGER,label TEXT);
CREATE TABLE custom_column_40(id INTEGER,value TEXT);
CREATE TABLE books_custom_column_40_link(id INTEGER PRIMARY KEY,book INTEGER,value INTEGER);
INSERT INTO custom_columns VALUES(40,'audiobookfiles');
INSERT INTO books VALUES(1,'Book','Author/Book (1)'),(2,'Only audio','Author/Only audio (2)'),(3,'No audio','Author/No audio (3)');
INSERT INTO custom_column_40 VALUES(1,'Recording A'||char(10)||'Recording B'),(2,'Recording B');
INSERT INTO books_custom_column_40_link(book,value) VALUES(1,1),(2,2);''');c.commit();c.close()
 def tearDown(self):self.temp.cleanup()
 def test_multiline_and_original_bytes(self):
  before=(self.lib/'metadata.db').read_bytes();p=m.build_plan(self.lib,self.audio);v=m.ReadOnlyView(p)
  self.assertEqual(p['virtual_books'],4)
  entries=[e for e in p['entries'] if e['id']==1];self.assertEqual(len(entries),2)
  for e in entries:
   path='/'+e['virtual']+'/book.epub';fh=v.open(path,os.O_RDONLY)
   try:self.assertEqual(v.read(path,100,0,fh),b'unchanged EPUB bytes')
   finally:v.release(path,fh)
   self.assertIn('metadata.opf',v.readdir('/'+e['virtual']))
  self.assertEqual(before,(self.lib/'metadata.db').read_bytes())
 def test_nested_audio_seek(self):
  p=m.build_plan(self.lib,self.audio);v=m.ReadOnlyView(p);e=p['entries'][0]
  path='/'+e['virtual']+'/audiobook/disc 1/01.mp3';fh=v.open(path,os.O_RDONLY)
  try:self.assertEqual(v.read(path,3,4,fh),b'456')
  finally:v.release(path,fh)
 def test_audio_only_and_ebook_only(self):
  p=m.build_plan(self.lib,self.audio);v=m.ReadOnlyView(p)
  self.assertIn('audiobook',v.readdir('/Author/Only audio (2)'))
  self.assertNotIn('audiobook',v.readdir('/Author/No audio (3)'))
 def test_write_denied(self):
  v=m.ReadOnlyView(m.build_plan(self.lib,self.audio));path='/Author/No audio (3)/book.pdf'
  for flag in [os.O_WRONLY,os.O_RDWR,os.O_TRUNC,os.O_CREAT]:
   with self.assertRaises(OSError) as err:v.open(path,flag)
   self.assertEqual(err.exception.errno,30)
 def test_escape(self):
  for rel in ['../outside','/etc/passwd','C:\\outside']:
   with self.assertRaises(ValueError):m.contained(self.audio,rel)
 def test_missing_audio(self):
  (self.audio/'Recording B/01.m4b').unlink();(self.audio/'Recording B').rmdir()
  p=m.build_plan(self.lib,self.audio);self.assertEqual(len(p['warnings']),2)
 def test_collision(self):
  (self.lib/'Author/Book (1)/audiobook').mkdir()
  with self.assertRaises(ValueError):m.build_plan(self.lib,self.audio)
 def test_filter(self):
  p=m.build_plan(self.lib,self.audio,ids={2});self.assertEqual(p['virtual_books'],1)
 def test_direct_custom_column(self):
  c=sqlite3.connect(self.lib/'metadata.db');c.executescript('DROP TABLE custom_column_40; CREATE TABLE custom_column_40(book INTEGER,value TEXT); INSERT INTO custom_column_40 VALUES(2,\'Recording B\');');c.close()
  p=m.build_plan(self.lib,self.audio);self.assertEqual(p['audio_entries'],1)
if __name__=='__main__':unittest.main(verbosity=2)
