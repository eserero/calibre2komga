import sqlite3
import re
from pathlib import Path
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

class CalibreMetadataStore:
    def __init__(self, calibre_path: Path):
        self.calibre_path = calibre_path
        self.metadata_db_path = self.calibre_path / 'metadata.db'
        self.metadata_cache = {}
        self.supported_formats = {'.epub', '.kepub'}

    def validate(self) -> bool:
        if not self.calibre_path.exists() or not self.calibre_path.is_dir():
            logger.error(f"Calibre library path is invalid: {self.calibre_path}")
            return False
        if not self.metadata_db_path.exists():
            logger.error(f"No metadata.db found in {self.calibre_path}")
            return False
        return True

    def load(self) -> bool:
        try:
            conn = sqlite3.connect(self.metadata_db_path)
            cursor = conn.cursor()
            
            query = """
            SELECT 
                b.id, b.title, b.path, b.series_index,
                a.name as author_name, s.name as series_name,
                GROUP_CONCAT(d.name, ',') as formats
            FROM books b
            LEFT JOIN books_authors_link bal ON b.id = bal.book
            LEFT JOIN authors a ON bal.author = a.id
            LEFT JOIN books_series_link bsl ON b.id = bsl.book
            LEFT JOIN series s ON bsl.series = s.id
            LEFT JOIN data d ON b.id = d.book
            GROUP BY b.id
            ORDER BY a.name, s.name, b.series_index, b.title
            """
            
            cursor.execute(query)
            for row in cursor.fetchall():
                book_id, title, path, series_index, author_name, series_name, formats = row
                if author_name:
                    author_name = author_name.split(',')[0].strip()
                
                self.metadata_cache[path] = {
                    'id': book_id,
                    'title': title,
                    'author': author_name or 'Unknown Author',
                    'series': series_name,
                    'series_index': series_index,
                    'formats': formats.split(',') if formats else []
                }
            conn.close()
            logger.info(f"Loaded metadata for {len(self.metadata_cache)} books")
            return True
        except Exception as e:
            logger.error(f"Error loading Calibre metadata: {e}")
            return False

    def clean_calibre_title(self, title: str) -> str:
        if not title:
            return title
        cleaned = re.sub(r'\s*\(\d+\)\s*$', '', title)
        cleaned = re.sub(r'\s+\(\d+\)\s*$', '', cleaned)
        return cleaned.strip()

    def sanitize_filename(self, filename: str) -> str:
        sanitized = re.sub(r'[<>:"/\\|?*]', '_', filename)
        sanitized = sanitized.strip('. ')
        if len(sanitized) > 100:
            sanitized = sanitized[:100]
        return sanitized

    def get_series_folder_name(self, metadata: Dict) -> str:
        author = metadata.get('author', 'Unknown Author')
        series = metadata.get('series')
        if series:
            return self.sanitize_filename(f"{author} - {series}")
        else:
            return self.sanitize_filename(f"{author}")

    def get_file_name(self, metadata: Dict, original_filename: str) -> str:
        title = metadata.get('title', 'Unknown Title')
        series_index = metadata.get('series_index')
        series = metadata.get('series')
        clean_title = self.clean_calibre_title(title)
        extension = Path(original_filename).suffix
        
        if series and series_index:
            if series_index == int(series_index):
                volume_num = str(int(series_index)).zfill(2)
            else:
                volume_num = f"{series_index:05.1f}".replace('.', '_')
            filename = f"Volume {volume_num} - {clean_title}"
        else:
            filename = clean_title
        
        return self.sanitize_filename(filename) + extension

    def find_ebook_files(self, book_path: Path) -> List[Path]:
        files = []
        if book_path.exists() and book_path.is_dir():
            for file_path in book_path.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in self.supported_formats:
                    files.append(file_path)
        return files
