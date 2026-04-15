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
        self.supported_formats = {'.epub', '.kepub', '.cbz', '.cbz', '.pdf', '.mobi', '.azw3'}

    def validate(self) -> bool:
        if not self.calibre_path.exists() or not self.calibre_path.is_dir():
            logger.error(f"Calibre library path is invalid: {self.calibre_path}")
            return False
        if not self.metadata_db_path.exists():
            logger.error(f"No metadata.db found in {self.calibre_path}")
            return False
        return True

    def load(self, audiobook_column: Optional[str] = None, audiobook_base_path: Optional[Path] = None) -> bool:
        try:
            conn = sqlite3.connect(self.metadata_db_path)
            cursor = conn.cursor()
            
            # Base query
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
            rows = cursor.fetchall()
            
            # Map column names to indexes for clarity
            # b.id, b.title, b.path, b.series_index, a.name, s.name, formats
            
            audiobook_map = {}
            if audiobook_column:
                # 1. Find the table name and type for the custom column
                cursor.execute("SELECT id, datatype FROM custom_columns WHERE label=?", (audiobook_column,))
                col_row = cursor.fetchone()
                if col_row:
                    col_id, datatype = col_row
                    col_table = f"custom_column_{col_id}"
                    
                    try:
                        # 2. Check the table structure to find the correct book-link column
                        # Some use 'book', some might use 'book_id' or require a link table
                        cursor.execute(f"PRAGMA table_info({col_table})")
                        columns = [c[1] for c in cursor.fetchall()]
                        
                        book_col = None
                        if 'book' in columns:
                            book_col = 'book'
                        elif 'book_id' in columns:
                            book_col = 'book_id'
                            
                        if book_col:
                            # Standard text/number column
                            cursor.execute(f"SELECT {book_col}, value FROM {col_table}")
                            for book_id, value in cursor.fetchall():
                                if value and str(value).strip():
                                    audiobook_map[book_id] = str(value).strip()
                        else:
                            # It might be a 'many-to-one' or 'text with fixed options' column
                            # These use a link table: books_custom_column_X_link
                            link_table = f"books_custom_column_{col_id}_link"
                            cursor.execute(f"SELECT l.book, v.value FROM {link_table} l JOIN {col_table} v ON l.value = v.id")
                            for book_id, value in cursor.fetchall():
                                if value and str(value).strip():
                                    audiobook_map[book_id] = str(value).strip()
                                    
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Could not read custom column table {col_table}: {e}")
                else:
                    logger.warning(f"Audiobook custom column '{audiobook_column}' not found in Calibre database.")

            for row in rows:
                book_id, title, path, series_index, author_name, series_name, formats = row
                if author_name:
                    author_name = author_name.split(',')[0].strip()
                
                audio_rel_path = audiobook_map.get(book_id)
                audio_abs_path = None
                if audio_rel_path:
                    # Normalize Windows-style backslashes to forward slashes for Linux
                    norm_rel_path = audio_rel_path.replace('\\', '/').lstrip('/')
                    
                    full_path = audiobook_base_path / norm_rel_path if audiobook_base_path else Path(norm_rel_path)
                    if full_path.exists() and full_path.is_dir():
                        audio_abs_path = full_path
                        logger.info(f"Resolved audiobook for '{title}': {audio_abs_path}")
                    else:
                        logger.warning(f"Audiobook path for '{title}' exists in DB ('{audio_rel_path}') but was not found on disk at: {full_path}")

                self.metadata_cache[path] = {
                    'id': book_id,
                    'title': title,
                    'author': author_name or 'Unknown Author',
                    'series': series_name,
                    'series_index': series_index,
                    'formats': formats.split(',') if formats else [],
                    'audiobook_path': audio_abs_path
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

    def get_virtual_segments(self, metadata: Dict, original_filename: str) -> List[str]:
        """
        Returns the path segments for the virtual filesystem.
        1. Series: Folder is just 'Series Name'.
        2. No Series: Folder is 'Author Name/_oneshot'.
        """
        series = metadata.get('series')
        author = metadata.get('author', 'Unknown Author')
        filename = self.get_file_name(metadata, original_filename)
        
        if series:
            # Series folder name is just the series name
            folder = self.sanitize_filename(series)
            return [folder, filename]
        else:
            # No series: Author Name / _oneshot / Filename
            author_folder = self.sanitize_filename(author)
            return [author_folder, "_oneshot", filename]

    def find_ebook_files(self, book_path: Path) -> List[Path]:
        files = []
        if book_path.exists() and book_path.is_dir():
            for file_path in book_path.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in self.supported_formats:
                    files.append(file_path)
        return files
