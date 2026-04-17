#!/usr/bin/env python3
"""
Calibre to Komga Integration Tool

This script integrates ebooks from Calibre's folder structure to Komga's expected format.
It supports both exporting (copying) files and mounting a virtual FUSE filesystem.
"""

import sys
import argparse
import logging
from pathlib import Path
from calibre_core import CalibreMetadataStore
from exporter import CalibreKomgaExporter

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_export(args, store):
    exporter = CalibreKomgaExporter(
        store=store,
        komga_path=args.komga_path,
        dry_run=args.dry_run
    )
    exporter.migrate_library(author_filter=args.author)

def run_mount(args, store):
    try:
        from fuse import FUSE
        from virtual_fs import KomgaFuse
        from crc_cache import CRCCache
    except ImportError:
        logger.error("The 'fusepy' library is required for mounting. Install it via: pip install fusepy")
        sys.exit(1)
        
    komga_path = Path(args.komga_path)
    if not komga_path.exists():
        komga_path.mkdir(parents=True, exist_ok=True)
        
    logger.info(f"Mounting FUSE filesystem at {args.komga_path}...")
    logger.info("Press Ctrl+C to unmount.")
    
    audiobook_exts = args.audiobook_exts.split(',') if getattr(args, 'audiobook_exts', None) else [".mp3", ".m4a"]
    
    # Setup CRC cache for audiobooks
    cache_path = Path.home() / ".cache" / "calibre2komga" / "crc_cache.db"
    crc_cache = CRCCache(cache_path)
    
    fuse_fs = KomgaFuse(
        store=store, 
        author_filter=args.author, 
        audiobook_exts=audiobook_exts,
        crc_cache=crc_cache
    )
    fuse_opts = {'nothreads': True, 'foreground': True}
    if args.allow_other:
        fuse_opts['allow_other'] = True
        
    FUSE(fuse_fs, str(args.komga_path), **fuse_opts)

def main():
    # To support backward compatibility with people calling `python calibre2komga.py path1 path2`,
    # we first check if there's no known subcommand in the args.
    subcommands = ['export', 'mount']
    argv = sys.argv[1:]
    
    # Check if a subcommand is provided. If not, default to 'export' for backward compatibility.
    if len(argv) >= 2 and argv[0] not in subcommands and not argv[0].startswith('-'):
        # It looks like they ran `calibre2komga.py /path/to/calibre /path/to/komga ...`
        argv = ['export'] + argv

    parser = argparse.ArgumentParser(
        description='Calibre to Komga Integration Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Legacy Export (Copying files)
  python calibre2komga.py export /path/to/calibre /path/to/komga
  
  # FUSE Mount (Virtual filesystem, no files copied)
  python calibre2komga.py mount /path/to/calibre /mnt/komga_virtual

  # FUSE Mount with Audiobook Injection
  python calibre2komga.py mount /path/to/calibre /mnt/komga_virtual \
    --audiobook-column "audiobook_path" \
    --audiobook-base-path /path/to/audiobooks
        '''
    )
    
    parser.add_argument('--verbose', action='store_true', help='Enable verbose logging')
    
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # Export subcommand
    export_parser = subparsers.add_parser('export', help='Copy ebooks to Komga folder structure')
    export_parser.add_argument('calibre_path', help='Path to Calibre library directory')
    export_parser.add_argument('komga_path', help='Path to destination Komga library directory')
    export_parser.add_argument('--dry-run', action='store_true', help='Show what would be migrated without actually copying files')
    export_parser.add_argument('--author', help='Filter by author name (case insensitive partial match)')
    
    # Mount subcommand
    mount_parser = subparsers.add_parser('mount', help='Mount virtual FUSE filesystem')
    mount_parser.add_argument('calibre_path', help='Path to Calibre library directory')
    mount_parser.add_argument('komga_path', help='Path to mount point')
    mount_parser.add_argument('--author', help='Filter by author name (case insensitive partial match)')
    mount_parser.add_argument('--allow-other', action='store_true', help='Allow other users to access the mount (required for Docker)')
    
    # Audiobook options for mount
    mount_parser.add_argument('--audiobook-column', help='Calibre custom column name containing the audiobook relative path')
    mount_parser.add_argument('--audiobook-base-path', help='Base path where audiobooks are stored')
    mount_parser.add_argument('--audiobook-exts', default='.mp3,.m4a', help='Comma-separated list of audiobook extensions (default: .mp3,.m4a)')
    
    args = parser.parse_args(argv)
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        
    store = CalibreMetadataStore(Path(args.calibre_path))
    if not store.validate():
        sys.exit(1)
        
    # Load metadata with audiobook info if provided
    audiobook_base_path = Path(args.audiobook_base_path) if getattr(args, 'audiobook_base_path', None) else None
    audiobook_column = getattr(args, 'audiobook_column', None)
    
    if not store.load(audiobook_column=audiobook_column, audiobook_base_path=audiobook_base_path):
        sys.exit(1)
        
    if args.command == 'export':
        run_export(args, store)
    elif args.command == 'mount':
        # Add audiobook_exts to args if they exist
        run_mount(args, store)

if __name__ == '__main__':
    main()
