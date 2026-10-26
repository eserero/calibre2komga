import importlib.util,os,sqlite3,tempfile,unittest,json,io
from contextlib import redirect_stderr
from unittest.mock import patch
from xml.sax.saxutils import quoteattr
from pathlib import Path
spec=importlib.util.spec_from_file_location('c2g',Path(__file__).with_name('calibre2grimmory.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.lib=self.root/'calibre';self.audio=self.root/'audio';self.lib.mkdir();self.audio.mkdir()
  for name in ['Author/Book (1)','Author/Only audio (2)','Author/No audio (3)']:(self.lib/name).mkdir(parents=True)
  (self.lib/'Author/Book (1)/book.epub').write_bytes(b'unchanged EPUB bytes')
  custom = ''.join('<meta name="calibre:user_metadata:#' + k + '" content=' + quoteattr(json.dumps({'#value#':v})) + '/>' for k,v in
                   dict(series2='Universe',readinglist='Summer',pages=321,abs_narrator=['One','Two'],category='Fantasy',readunread=True,words=90000).items())
  self.opf = '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
<dc:title>Book &amp; Story</dc:title><dc:creator opf:role="aut">Author</dc:creator><dc:creator opf:role="edt">Editor</dc:creator>
<dc:description>&lt;p&gt;Description&lt;/p&gt;</dc:description><dc:language>eng</dc:language><dc:date>0101-01-01T00:00:00Z</dc:date>
<dc:subject>Fantasy</dc:subject><dc:identifier opf:scheme="isbn">978-0-306-40615-7</dc:identifier><dc:identifier opf:scheme="goodreads">123</dc:identifier>
<meta name="calibre:series" content="Series"/><meta name="calibre:series_index" content="1.5"/>''' + custom + '</metadata></package>'
  for name in ['Author/Book (1)','Author/Only audio (2)','Author/No audio (3)']:
   (self.lib/name/'metadata.opf').write_text(self.opf,encoding='utf-8')
   (self.lib/name/'cover.jpg').write_bytes(b'cover bytes')
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
 def test_metadata_conversion(self):
  d=m.opf_metadata(self.lib/'Author/Book (1)/metadata.opf')
  self.assertEqual(d['title'],'Book & Story');self.assertEqual(d['authors'],['Author'])
  self.assertEqual(d['series'],{'name':'Series','number':1.5})
  self.assertEqual(d['tags'],['Fantasy','Secondary series: Universe','Reading list: Summer'])
  self.assertEqual(d['description'],'<p>Description</p>');self.assertEqual(d['language'],'en')
  self.assertEqual(d['pageCount'],321);self.assertEqual(d['narrator'],'One & Two')
  self.assertEqual(d['isbn13'],'9780306406157');self.assertEqual(d['identifiers'],{'goodreadsId':'123'})
  self.assertNotIn('publishedDate',d);self.assertNotIn('readunread',d);self.assertNotIn('words',d)
 def test_virtual_sidecars_and_sizes(self):
  before={str(p):p.read_bytes() for root in [self.lib,self.audio] for p in root.rglob('*') if p.is_file()}
  plan=m.build_plan(self.lib,self.audio);v=m.ReadOnlyView(plan)
  self.assertGreater(v.stats['virtual_json_bytes'],v.stats['cached_json_bytes'])
  self.assertEqual(v.stats['opf_files'],3)
  for e in plan['entries']:
   folder='/'+e['virtual']
   key=folder+('/audiobook.metadata.json' if e['audio_source'] else '/book.metadata.json')
   self.assertIn(key.rsplit('/',1)[1],v.readdir(folder))
   fh=v.open(key,os.O_RDONLY)
   try:
    data=v.read(key,99999,0,fh)
    self.assertEqual(len(data),v.getattr(key)['st_size'])
    self.assertEqual(v.read(key,8,7,fh),data[7:15])
    self.assertEqual(v.read(key,8,len(data),fh),b'')
    self.assertEqual(json.loads(data)['metadata']['series']['number'],1.5)
   finally:v.release(key,fh)
   with self.assertRaises(OSError):v.open(key,os.O_WRONLY)
  nested='/'+plan['entries'][0]['virtual']+'/audiobook/disc 1/01.metadata.json'
  self.assertIn('01.metadata.json',v.readdir(nested.rsplit('/',1)[0]))
  cover='/'+plan['entries'][0]['virtual']+'/book.cover.jpg';fh=v.open(cover,os.O_RDONLY)
  try:self.assertEqual(v.read(cover,99,0,fh),b'cover bytes')
  finally:v.release(cover,fh)
  self.assertEqual(before,{str(p):p.read_bytes() for root in [self.lib,self.audio] for p in root.rglob('*') if p.is_file()})
 def test_sidecar_collision_and_optout(self):
  (self.lib/'Author/No audio (3)/book.metadata.json').write_text('{}')
  with self.assertLogs(m.LOG,level='WARNING'):
   generated=m.ReadOnlyView(m.build_plan(self.lib,self.audio))
  self.assertEqual(generated.stats['existing_files_overlaid'],1)
  self.assertEqual((self.lib/'Author/No audio (3)/book.metadata.json').read_text(),'{}')
  self.assertEqual(json.loads(generated.generated['Author/No audio (3)/book.metadata.json'])['metadata']['title'],'Book & Story')
  v=m.ReadOnlyView(m.build_plan(self.lib,self.audio),sidecars=False)
  self.assertEqual(v.generated,{})
 def test_missing_and_invalid_opf(self):
  (self.lib/'Author/No audio (3)/metadata.opf').unlink()
  (self.lib/'Author/Only audio (2)/metadata.opf').write_text('bad')
  with self.assertLogs(m.LOG,level='WARNING'):
   v=m.ReadOnlyView(m.build_plan(self.lib,self.audio))
  self.assertEqual(v.stats['skipped_opfs'],2)
  self.assertIn('book.pdf',v.readdir('/Author/No audio (3)'))
 def test_snapshot_refresh_on_restart(self):
  plan=m.build_plan(self.lib,self.audio);v=m.ReadOnlyView(plan)
  path=self.lib/'Author/Book (1)/metadata.opf'
  path.write_text(self.opf.replace('1.5','2.5'),encoding='utf-8')
  refreshed=m.ReadOnlyView(plan)
  key=plan['entries'][0]['virtual']+'/book.metadata.json'
  self.assertEqual(json.loads(v.generated[key])['metadata']['series']['number'],1.5)
  self.assertEqual(json.loads(refreshed.generated[key])['metadata']['series']['number'],2.5)
 def cached_view(self):
  cache=m.SidecarCache(self.root/'cache',[self.lib,self.audio])
  return m.ReadOnlyView(m.build_plan(self.lib,self.audio),disk_cache=cache)
 def test_cache_warm_and_one_changed_opf(self):
  first=self.cached_view();self.assertEqual(first.stats['cache_rebuilt'],3)
  before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in (self.root/'cache').iterdir()}
  with patch.object(m,'opf_metadata',side_effect=AssertionError('Unchanged OPF parsed')), patch.object(Path,'read_bytes',side_effect=AssertionError('Cached JSON eagerly read')):
   second=self.cached_view()
  self.assertEqual(second.stats['cache_hits'],3);self.assertEqual(second.stats['cache_rebuilt'],0)
  self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in (self.root/'cache').iterdir()})
  path=self.lib/'Author/Book (1)/metadata.opf';path.write_text(self.opf.replace('1.5','2.5'),encoding='utf-8')
  third=self.cached_view();self.assertEqual(third.stats['cache_hits'],2);self.assertEqual(third.stats['cache_rebuilt'],1)
  changed=[p.name for p in (self.root/'cache').glob('*.metadata.json') if before[p.name][0]!=p.read_bytes()]
  self.assertEqual(len(changed),1)
 def test_cache_lazy_reads_and_stable_times(self):
  v=self.cached_view();self.assertEqual(v.generated,{})
  entry=next(e for e in v.entries.values() if e['id']==1)
  folder='/'+entry['virtual'];path=folder+'/book.metadata.json'
  self.assertIn('book.metadata.json',v.readdir(folder))
  self.assertIn('audiobook.metadata.json',v.readdir(folder))
  before=v.getattr(path)['st_mtime'];fh=v.open(path,os.O_RDONLY)
  try:
   data=v.read(path,99999,0,fh);self.assertEqual(json.loads(data)['metadata']['series']['number'],1.5)
   self.assertEqual(v.read(path,7,3,fh),data[3:10])
  finally:v.release(path,fh)
  audio=folder+'/audiobook/disc 1/01.metadata.json'
  self.assertGreater(v.getattr(audio)['st_size'],0)
  self.assertIn('disc 1.metadata.json',v.readdir(folder+'/audiobook'))
  cover=folder+'/book.cover.jpg';fh=v.open(cover,os.O_RDONLY)
  try:self.assertEqual(v.read(cover,99,0,fh),b'cover bytes')
  finally:v.release(cover,fh)
  self.assertEqual(self.cached_view().getattr(path)['st_mtime'],before)
 def test_cache_repairs_and_deleted_opf(self):
  v=self.cached_view();f=next((self.root/'cache').glob('*.metadata.json'));f.write_text('broken')
  repaired=self.cached_view()
  with self.assertLogs(m.LOG,level='WARNING'):
   next(p for p in repaired.lazy_metadata.values() if p.target==f).materialize()
  self.assertEqual(repaired.disk_cache.rebuilt,1)
  f.unlink();repaired=self.cached_view()
  with self.assertLogs(m.LOG,level='WARNING'):
   next(p for p in repaired.lazy_metadata.values() if p.target==f).materialize()
  self.assertEqual(repaired.disk_cache.rebuilt,1)
  (self.lib/'Author/No audio (3)/metadata.opf').unlink()
  with self.assertLogs(m.LOG,level='WARNING'):v=self.cached_view()
  self.assertNotIn('book.metadata.json',v.readdir('/Author/No audio (3)'))
 def test_cache_version_and_separation(self):
  self.cached_view()
  with patch.object(m,'CACHE_VERSION',m.CACHE_VERSION+1):
   self.assertEqual(self.cached_view().stats['cache_rebuilt'],3)
  for invalid in [self.lib,self.lib/'cache',self.root]:
   with self.assertRaises(ValueError):m.SidecarCache(invalid,[self.lib,self.audio])
  unrelated=self.root/'unrelated';unrelated.mkdir();(unrelated/'keep').write_text('keep')
  with self.assertRaises(ValueError):m.SidecarCache(unrelated,[self.lib,self.audio])
  self.assertEqual((unrelated/'keep').read_text(),'keep')
 def test_cli_conversion_is_opt_in(self):
  output=self.root/'preview.json'
  argv=['calibre2grimmory.py','preview','--calibre',str(self.lib),'--audio-base',str(self.audio),'--output',str(output)]
  with patch.object(m.sys,'argv',argv), patch.object(m,'opf_metadata',side_effect=AssertionError('Default must not convert')), redirect_stderr(io.StringIO()):
   self.assertEqual(m.main(),0)
  self.assertEqual(json.loads(output.read_text())['sidecar_summary'],{})
  with patch.object(m.sys,'argv',argv+['--sidecar-cache',str(self.root/'cache')]),redirect_stderr(io.StringIO()):
   self.assertEqual(m.main(),0)
  self.assertEqual(json.loads(output.read_text())['sidecar_summary']['cache_rebuilt'],3)
if __name__=='__main__':unittest.main(verbosity=2)
