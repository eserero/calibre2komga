import os
import stat
import errno
import logging
from fuse import FuseOSError, Operations
from pathlib import Path
from typing import Optional
from calibre_core import CalibreMetadataStore

logger = logging.getLogger(__name__)

class KomgaFuse(Operations):
    def __init__(self, store: CalibreMetadataStore, author_filter: Optional[str] = None):
        self.store = store
        self.author_filter = author_filter
        
        # In-memory virtual tree
        # structure: { "directory_name": { "filename.epub": "/real/path/to/file.epub" } }
        self.virtual_tree = {}
        self.fd = 0
        self._build_tree()

    def _build_tree(self):
        logger.info("Building virtual directory tree...")
        for path_key, metadata in self.store.metadata_cache.items():
            if self.author_filter and self.author_filter.lower() not in metadata['author'].lower():
                continue
                
            book_path = self.store.calibre_path / path_key
            ebook_files = self.store.find_ebook_files(book_path)
            
            if not ebook_files:
                continue
                
            series_folder = self.store.get_series_folder_name(metadata)
            
            if series_folder not in self.virtual_tree:
                self.virtual_tree[series_folder] = {}
                
            for ebook_file in ebook_files:
                new_filename = self.store.get_file_name(metadata, ebook_file.name)
                self.virtual_tree[series_folder][new_filename] = str(ebook_file)
                
        logger.info(f"Virtual tree built with {len(self.virtual_tree)} directories.")

    def getattr(self, path, fh=None):
        if path == '/':
            return {
                'st_mode': (stat.S_IFDIR | 0o555),
                'st_nlink': 2,
            }
            
        parts = path.strip('/').split('/')
        
        if len(parts) == 1:
            # Series directory
            dirname = parts[0]
            if dirname in self.virtual_tree:
                return {
                    'st_mode': (stat.S_IFDIR | 0o555),
                    'st_nlink': 2,
                }
        elif len(parts) == 2:
            # File
            dirname, filename = parts
            if dirname in self.virtual_tree and filename in self.virtual_tree[dirname]:
                real_path = self.virtual_tree[dirname][filename]
                try:
                    real_stat = os.stat(real_path)
                    return {
                        'st_mode': (stat.S_IFREG | 0o444),
                        'st_nlink': 1,
                        'st_size': real_stat.st_size,
                        'st_mtime': real_stat.st_mtime,
                        'st_ctime': real_stat.st_ctime,
                        'st_atime': real_stat.st_atime,
                    }
                except OSError:
                    raise FuseOSError(errno.ENOENT)
                    
        raise FuseOSError(errno.ENOENT)

    def readdir(self, path, fh):
        dirents = ['.', '..']
        if path == '/':
            dirents.extend(self.virtual_tree.keys())
        else:
            parts = path.strip('/').split('/')
            if len(parts) == 1:
                dirname = parts[0]
                if dirname in self.virtual_tree:
                    dirents.extend(self.virtual_tree[dirname].keys())
                else:
                    raise FuseOSError(errno.ENOENT)
            else:
                raise FuseOSError(errno.ENOTDIR)
        
        for r in dirents:
            yield r

    def open(self, path, flags):
        parts = path.strip('/').split('/')
        if len(parts) != 2:
            raise FuseOSError(errno.ENOENT)
            
        dirname, filename = parts
        if dirname in self.virtual_tree and filename in self.virtual_tree[dirname]:
            real_path = self.virtual_tree[dirname][filename]
            # Ensure it's read-only
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
                raise FuseOSError(errno.EROFS)
            
            return os.open(real_path, flags)
            
        raise FuseOSError(errno.ENOENT)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def release(self, path, fh):
        return os.close(fh)
