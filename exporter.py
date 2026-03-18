import shutil
import logging
from pathlib import Path
from typing import Optional
from calibre_core import CalibreMetadataStore

logger = logging.getLogger(__name__)

class CalibreKomgaExporter:
    def __init__(self, store: CalibreMetadataStore, komga_path: str, dry_run: bool = False):
        self.store = store
        self.komga_path = Path(komga_path)
        self.dry_run = dry_run
        self.stats = {'total_books': 0, 'migrated_books': 0, 'skipped_books': 0, 'errors': 0}

    def validate(self) -> bool:
        if not self.dry_run:
            self.komga_path.mkdir(parents=True, exist_ok=True)
        return True

    def migrate_library(self, author_filter: Optional[str] = None):
        logger.info(f"Starting migration to {self.komga_path}")
        if not self.validate():
            return

        for path_key, metadata in self.store.metadata_cache.items():
            book_path = self.store.calibre_path / path_key
            
            if author_filter and author_filter.lower() not in metadata['author'].lower():
                continue
            
            self.stats['total_books'] += 1
            self._migrate_book(book_path, metadata)
            
        self._print_summary()

    def _migrate_book(self, book_path: Path, metadata: dict) -> bool:
        ebook_files = self.store.find_ebook_files(book_path)
        if not ebook_files:
            logger.warning(f"No supported ebook files found in {book_path}")
            self.stats['skipped_books'] += 1
            return False

        series_folder = self.store.get_series_folder_name(metadata)
        series_path = self.komga_path / series_folder

        series_info = ""
        if metadata.get('series'):
            series_info = f" (Series: {metadata['series']}"
            if metadata.get('series_index'):
                series_info += f", Index: {metadata['series_index']}"
            series_info += ")"
            
        logger.info(f"Migrating: {metadata['author']}/{metadata['title']}{series_info} -> {series_folder}/")
        
        if not self.dry_run:
            try:
                series_path.mkdir(parents=True, exist_ok=True)
                for ebook_file in ebook_files:
                    new_filename = self.store.get_file_name(metadata, ebook_file.name)
                    dest_file = series_path / new_filename
                    if dest_file.exists():
                        logger.warning(f"File already exists, skipping: {dest_file}")
                        continue
                    shutil.copy2(ebook_file, dest_file)
                self.stats['migrated_books'] += 1
                return True
            except Exception as e:
                logger.error(f"Error migrating {book_path}: {str(e)}")
                self.stats['errors'] += 1
                return False
        else:
            for ebook_file in ebook_files:
                new_filename = self.store.get_file_name(metadata, ebook_file.name)
                logger.info(f"[DRY RUN] Would create: {series_path / new_filename}")
            self.stats['migrated_books'] += 1
            return True

    def _print_summary(self):
        logger.info("Migration Summary:")
        logger.info(f"  Total books found: {self.stats['total_books']}")
        logger.info(f"  Books migrated: {self.stats['migrated_books']}")
        logger.info(f"  Books skipped: {self.stats['skipped_books']}")
        logger.info(f"  Errors: {self.stats['errors']}")
        
        if self.stats['total_books'] > 0:
            success_rate = (self.stats['migrated_books'] / self.stats['total_books']) * 100
            logger.info(f"  Success rate: {success_rate:.1f}%")
