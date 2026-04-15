import os
import struct
import logging
import zlib
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class VirtualZipMapper:
    """
    Virtually merges an existing ZIP file with an external directory.
    Exposes a flat byte-range 'read' API that makes the combination look like a single valid ZIP.
    """
    
    # ZIP Constants
    LFH_SIGNATURE = b'PK\x03\x04'
    CDH_SIGNATURE = b'PK\x01\x02'
    EOCD_SIGNATURE = b'PK\x05\x06'
    
    LFH_SIZE = 30
    CDH_SIZE = 46
    EOCD_SIZE = 22

    def __init__(self, base_zip_path: str, external_dir: str, target_dir: str = "audiobook", allowed_exts: List[str] = [".mp3", ".m4a"]):
        self.base_zip_path = Path(base_zip_path)
        self.external_dir = Path(external_dir)
        self.target_dir = target_dir.strip("/")
        self.allowed_exts = [ext.lower() for ext in allowed_exts]
        
        self.base_size = self.base_zip_path.stat().st_size
        self.external_files: List[Dict] = []
        self.extents: List[Tuple[int, int, Any]] = [] # (virtual_start, length, source_info)
        self.virtual_size = 0
        
        self._initialize()

    def _initialize(self):
        # 1. Parse base ZIP EOCD and Central Directory
        base_cd_offset, base_cd_size, base_cd_entries = self._parse_base_eocd()
        
        # 2. Scan external directory
        self._scan_external_dir()
        
        # 3. Build extents
        # Extent 1: Original File Data (everything up to the original CD)
        self.extents.append((0, base_cd_offset, ("file", str(self.base_zip_path), 0)))
        current_voffset = base_cd_offset
        
        # Extent 2..N: External Files (LFH + Data)
        new_cd_entries_data = []
        
        # We need the original CD entries too
        with open(self.base_zip_path, 'rb') as f:
            f.seek(base_cd_offset)
            original_cd_data = f.read(base_cd_size)
        
        for ext_file in self.external_files:
            rel_path = f"{self.target_dir}/{ext_file['name']}"
            
            # Calculate CRC32
            with open(ext_file['path'], 'rb') as f:
                crc = zlib.crc32(f.read())
            
            # Create LFH
            lfh = self._make_lfh(rel_path, ext_file['size'], ext_file['mtime'], crc)
            lfh_len = len(lfh)
            
            # Map LFH
            self.extents.append((current_voffset, lfh_len, ("mem", lfh)))
            lfh_voffset = current_voffset
            current_voffset += lfh_len
            
            # Map File Data
            self.extents.append((current_voffset, ext_file['size'], ("file", str(ext_file['path']), 0)))
            current_voffset += ext_file['size']
            
            # Create CDH entry for this file
            cdh = self._make_cdh(rel_path, ext_file['size'], ext_file['mtime'], lfh_voffset, crc)
            new_cd_entries_data.append(cdh)

        # Extent Central Directory: Original CD + New CD entries
        combined_cd = original_cd_data + b"".join(new_cd_entries_data)
        cd_voffset = current_voffset
        cd_size = len(combined_cd)
        self.extents.append((cd_voffset, cd_size, ("mem", combined_cd)))
        current_voffset += cd_size
        
        # Extent EOCD: Updated EOCD
        total_entries = base_cd_entries + len(self.external_files)
        eocd = self._make_eocd(total_entries, cd_size, cd_voffset)
        self.extents.append((current_voffset, len(eocd), ("mem", eocd)))
        current_voffset += len(eocd)
        
        self.virtual_size = current_voffset

    def _parse_base_eocd(self) -> Tuple[int, int, int]:
        """Finds CD offset, CD size, and entry count from base ZIP."""
        with open(self.base_zip_path, 'rb') as f:
            # ZIP files can have comments, so we search from the end for the EOCD signature
            f.seek(0, os.SEEK_END)
            filesize = f.tell()
            search_range = min(filesize, 65536 + self.EOCD_SIZE)
            f.seek(filesize - search_range)
            data = f.read(search_range)
            
            eocd_pos = data.rfind(self.EOCD_SIGNATURE)
            if eocd_pos == -1:
                raise ValueError(f"Not a valid ZIP file: {self.base_zip_path}")
            
            eocd_data = data[eocd_pos:eocd_pos + self.EOCD_SIZE]
            num_entries = struct.unpack('<H', eocd_data[10:12])[0]
            cd_size = struct.unpack('<I', eocd_data[12:16])[0]
            cd_offset = struct.unpack('<I', eocd_data[16:20])[0]
            
            return cd_offset, cd_size, num_entries

    def _scan_external_dir(self):
        if not self.external_dir.is_dir():
            return
            
        for entry in sorted(self.external_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() in self.allowed_exts:
                stat = entry.stat()
                self.external_files.append({
                    'name': entry.name,
                    'path': entry,
                    'size': stat.st_size,
                    'mtime': int(stat.st_mtime)
                })

    def _dos_datetime(self, timestamp: int) -> Tuple[int, int]:
        import datetime
        dt = datetime.datetime.fromtimestamp(timestamp)
        dos_date = ((dt.year - 1980) << 9) | (dt.month << 5) | dt.day
        dos_time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
        return dos_time, dos_date

    def _make_lfh(self, filename: str, size: int, mtime: int, crc: int) -> bytes:
        filename_bytes = filename.encode('utf-8')
        dos_time, dos_date = self._dos_datetime(mtime)
        
        return struct.pack('<4s5H3I2H', 
            self.LFH_SIGNATURE,
            20, # Version needed to extract (2.0)
            0x800, # General purpose bit flag (UTF-8)
            0,  # Compression method (0 = store)
            dos_time,
            dos_date,
            crc,
            size, # Compressed size
            size, # Uncompressed size
            len(filename_bytes),
            0   # Extra field length
        ) + filename_bytes

    def _make_cdh(self, filename: str, size: int, mtime: int, lfh_offset: int, crc: int) -> bytes:
        filename_bytes = filename.encode('utf-8')
        dos_time, dos_date = self._dos_datetime(mtime)
        
        return struct.pack('<4s6H3I5H2I',
            self.CDH_SIGNATURE,
            20, # Version made by
            20, # Version needed to extract
            0x800, # General purpose bit flag (UTF-8)
            0,  # Compression method (0 = store)
            dos_time,
            dos_date,
            crc,
            size, # Compressed size
            size, # Uncompressed size
            len(filename_bytes),
            0, # Extra field length
            0, # File comment length
            0, # Disk number start
            0, # Internal file attributes
            0x81A40000, # External file attributes (regular file, 0644)
            lfh_offset
        ) + filename_bytes

    def _make_eocd(self, total_entries: int, cd_size: int, cd_offset: int) -> bytes:
        return struct.pack('<4s4H2IH',
            self.EOCD_SIGNATURE,
            0, # Number of this disk
            0, # Disk where central directory starts
            total_entries, # Number of central directory records on this disk
            total_entries, # Total number of central directory records
            cd_size,
            cd_offset,
            0  # ZIP file comment length
        )

    def get_virtual_size(self) -> int:
        return self.virtual_size

    def read(self, offset: int, length: int) -> bytes:
        if offset >= self.virtual_size:
            return b""
        
        length = min(length, self.virtual_size - offset)
        result = bytearray()
        
        remaining = length
        current_offset = offset
        
        for start, extent_len, source in self.extents:
            if current_offset < start + extent_len and current_offset + remaining > start:
                overlap_start = max(current_offset, start)
                overlap_end = min(current_offset + remaining, start + extent_len)
                overlap_len = overlap_end - overlap_start
                
                source_offset = overlap_start - start
                
                if source[0] == "file":
                    path, base_file_offset = source[1], source[2]
                    with open(path, 'rb') as f:
                        f.seek(base_file_offset + source_offset)
                        result.extend(f.read(overlap_len))
                elif source[0] == "mem":
                    data = source[1]
                    result.extend(data[source_offset:source_offset + overlap_len])
                
                current_offset += overlap_len
                remaining -= overlap_len
                
            if remaining <= 0:
                break
                
        return bytes(result)

if __name__ == "__main__":
    import argparse
    from fuse import FUSE, Operations, FuseOSError
    import errno
    import stat

    class SingleFileFuse(Operations):
        def __init__(self, mapper: VirtualZipMapper, filename: str):
            self.mapper = mapper
            self.filename = filename
            self.virtual_size = mapper.get_virtual_size()

        def getattr(self, path, fh=None):
            if path == '/':
                return dict(st_mode=(stat.S_IFDIR | 0o555), st_nlink=2)
            if path == '/' + self.filename:
                return dict(st_mode=(stat.S_IFREG | 0o444), st_nlink=1,
                            st_size=self.virtual_size)
            raise FuseOSError(errno.ENOENT)

        def readdir(self, path, fh):
            return ['.', '..', self.filename]

        def open(self, path, flags):
            if path != '/' + self.filename:
                raise FuseOSError(errno.ENOENT)
            if flags & (os.O_WRONLY | os.O_RDWR):
                raise FuseOSError(errno.EROFS)
            return 0

        def read(self, path, length, offset, fh):
            return self.mapper.read(offset, length)

    parser = argparse.ArgumentParser()
    parser.add_argument("base_zip")
    parser.add_argument("extra_dir")
    parser.add_argument("mountpoint")
    parser.add_argument("--target-dir", default="audiobook")
    parser.add_argument("--exts", default=".mp3,.m4a")
    args = parser.parse_args()

    mapper = VirtualZipMapper(args.base_zip, args.extra_dir, args.target_dir, args.exts.split(','))
    filename = os.path.basename(args.base_zip)
    
    print(f"Mounting virtual {filename} at {args.mountpoint}")
    FUSE(SingleFileFuse(mapper, filename), args.mountpoint, foreground=True, nothreads=True)
