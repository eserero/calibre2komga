import sqlite3
import zlib
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class CRCCache:
    def __init__(self, cache_db_path: Path):
        self.cache_db_path = cache_db_path
        self._init_db()

    def _init_db(self):
        self.cache_db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crc_cache (
                    file_path TEXT PRIMARY KEY,
                    file_size INTEGER,
                    mtime INTEGER,
                    crc32 INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_path ON crc_cache(file_path)")

    def get_crc(self, file_path: Path) -> Optional[int]:
        try:
            stat = file_path.stat()
            size = stat.st_size
            mtime = int(stat.st_mtime)
            
            with sqlite3.connect(self.cache_db_path) as conn:
                cursor = conn.execute(
                    "SELECT crc32 FROM crc_cache WHERE file_path = ? AND file_size = ? AND mtime = ?",
                    (str(file_path), size, mtime)
                )
                row = cursor.fetchone()
                if row:
                    return row[0]
        except Exception as e:
            logger.debug(f"Cache miss or error for {file_path}: {e}")
        return None

    def set_crc(self, file_path: Path, crc32: int):
        try:
            stat = file_path.stat()
            size = stat.st_size
            mtime = int(stat.st_mtime)
            
            with sqlite3.connect(self.cache_db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO crc_cache (file_path, file_size, mtime, crc32) VALUES (?, ?, ?, ?)",
                    (str(file_path), size, mtime, crc32)
                )
        except Exception as e:
            logger.warning(f"Failed to cache CRC for {file_path}: {e}")

    def get_or_calculate(self, file_path: Path) -> int:
        crc = self.get_crc(file_path)
        if crc is not None:
            return crc
            
        logger.info(f"Calculating CRC32 for {file_path.name} (first time)...")
        crc = 0
        with open(file_path, 'rb') as f:
            # Read in chunks to avoid memory issues for very large files
            while True:
                chunk = f.read(1024 * 1024) # 1MB chunks
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
        
        # Ensure it's an unsigned 32-bit integer
        crc = crc & 0xFFFFFFFF
        self.set_crc(file_path, crc)
        return crc
