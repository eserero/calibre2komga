import os
import stat
import errno
import logging
from fuse import FuseOSError, Operations
from pathlib import Path
from typing import Optional, Dict, Any, List
from calibre_core import CalibreMetadataStore

logger = logging.getLogger(__name__)

class KomgaFuse(Operations):
    def __init__(self, store: CalibreMetadataStore, author_filter: Optional[str] = None):
        self.store = store
        self.author_filter = author_filter
        
        # In-memory virtual tree
        # structure: { "folder": { "subfolder": { "file.epub": "/real/path" } } }
        # Files are strings (real paths), directories are dictionaries.
        self.virtual_tree = {}
        self._build_tree()

    def _add_to_tree(self, segments: List[str], real_path: str):
        """Helper to add a list of path segments to the nested tree."""
        current = self.virtual_tree
        for i, segment in enumerate(segments):
            if i == len(segments) - 1:
                # Last segment is the file
                current[segment] = real_path
            else:
                # Intermediate segments are directories
                if segment not in current:
                    current[segment] = {}
                elif not isinstance(current[segment], dict):
                    # Collision: filename same as directory name
                    # We'll rename the directory slightly to avoid collision if necessary,
                    # but for now we just log a warning.
                    logger.warning(f"Namespace collision at segment '{segment}'")
                    continue
                current = current[segment]

    def _build_tree(self):
        logger.info("Building nested virtual directory tree...")
        for path_key, metadata in self.store.metadata_cache.items():
            if self.author_filter and self.author_filter.lower() not in metadata['author'].lower():
                continue
                
            book_path = self.store.calibre_path / path_key
            ebook_files = self.store.find_ebook_files(book_path)
            
            for ebook_file in ebook_files:
                # Get segments using the new logic in calibre_core
                segments = self.store.get_virtual_segments(metadata, ebook_file.name)
                self._add_to_tree(segments, str(ebook_file))
                
        logger.info("Virtual tree built successfully.")

    def _get_node(self, path: str) -> Any:
        """Helper to find a node in the tree based on the virtual path."""
        if path == '/':
            return self.virtual_tree
            
        parts = path.strip('/').split('/')
        current = self.virtual_tree
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def getattr(self, path, fh=None):
        node = self._get_node(path)
        if node is None:
            raise FuseOSError(errno.ENOENT)
            
        if isinstance(node, dict):
            # It's a directory
            return {
                'st_mode': (stat.S_IFDIR | 0o555),
                'st_nlink': 2,
            }
        else:
            # It's a file (node is the real path string)
            try:
                real_stat = os.stat(node)
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

    def readdir(self, path, fh):
        node = self._get_node(path)
        if node is None or not isinstance(node, dict):
            raise FuseOSError(errno.ENOENT)
            
        dirents = ['.', '..']
        dirents.extend(node.keys())
        
        for r in dirents:
            yield r

    def open(self, path, flags):
        node = self._get_node(path)
        if node is None or isinstance(node, dict):
            raise FuseOSError(errno.ENOENT)
            
        # Ensure it's read-only
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC):
            raise FuseOSError(errno.EROFS)
            
        return os.open(node, flags)

    def read(self, path, length, offset, fh):
        os.lseek(fh, offset, os.SEEK_SET)
        return os.read(fh, length)

    def release(self, path, fh):
        return os.close(fh)
