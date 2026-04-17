#!/bin/bash
# test_cache.sh - Verifies CRC Caching behavior (Persistence)

# Set up environment
TEST_ROOT="cache_test_env"
MOCK_CALIBRE="$TEST_ROOT/mock_calibre"
MOCK_AUDIO="$TEST_ROOT/mock_audiobooks"
MOUNT_DIR="$TEST_ROOT/mock_mount"
VENV_DIR="venv"
CACHE_DB="$HOME/.cache/calibre2komga/crc_cache.db"

# Cleanup from previous runs
rm -rf "$TEST_ROOT"
rm -f "$CACHE_DB"

mkdir -p "$MOCK_CALIBRE/Author Name/Book Title (1)"
mkdir -p "$MOCK_AUDIO/my_audiobook"
mkdir -p "$MOUNT_DIR"

# 1. Create Mock Files
echo "Audio Data" > "$MOCK_AUDIO/my_audiobook/audio1.mp3"
python3 -c "import zipfile; z = zipfile.ZipFile('$MOCK_CALIBRE/Author Name/Book Title (1)/Book Title.epub', 'w'); z.writestr('mimetype', 'application/epub+zip'); z.close()"

# 2. Create Mock Database
python3 <<EOF
import sqlite3
conn = sqlite3.connect('$MOCK_CALIBRE/metadata.db')
c = conn.cursor()
c.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, path TEXT, series_index REAL DEFAULT 1.0)")
c.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)")
c.execute("CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER)")
c.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT)")
c.execute("CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT)")
c.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT)")
c.execute("CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER)")
c.execute("INSERT INTO custom_columns (id, label, name, datatype) VALUES (1, 'audiobook_path', 'Audiobook Path', 'text')")
c.execute("CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, book INTEGER, value TEXT)")
c.execute("INSERT INTO books (id, title, path) VALUES (1, 'Book Title', 'Author Name/Book Title (1)')")
c.execute("INSERT INTO authors (id, name) VALUES (1, 'Author Name')")
c.execute("INSERT INTO books_authors_link (book, author) VALUES (1, 1)")
c.execute("INSERT INTO data (book, format, name) VALUES (1, 'EPUB', 'Book Title')")
c.execute("INSERT INTO series (id, name) VALUES (1, 'Test Series')")
c.execute("INSERT INTO books_series_link (book, series) VALUES (1, 1)")
c.execute("INSERT INTO custom_column_1 (book, value) VALUES (1, 'my_audiobook')")
conn.commit()
conn.close()
EOF

source "$VENV_DIR/bin/activate"

# Function to run mount and read
run_test_pass() {
    local pass_num=$1
    echo "--- PASS $pass_num ---"
    
    python3 calibre2komga.py mount "$MOCK_CALIBRE" "$MOUNT_DIR" \
      --audiobook-column "audiobook_path" \
      --audiobook-base-path "$MOCK_AUDIO" > "$TEST_ROOT/pass$pass_num.log" 2>&1 &
    local pid=$!
    
    # Wait for mount
    sleep 2
    
    # Trigger read
    unzip -l "$MOUNT_DIR/Test Series/Volume 01 - Book Title.epub" > /dev/null
    
    # Cleanup mount
    fusermount -u "$MOUNT_DIR"
    kill $pid || true
    wait $pid 2>/dev/null || true
}

# RUN 1: Should calculate CRC
run_test_pass 1
if grep -q "Calculating CRC32" "$TEST_ROOT/pass1.log"; then
    echo "✅ Pass 1: Calculated CRC as expected."
else
    echo "❌ Pass 1 ERROR: Did not log 'Calculating CRC32'."
    exit 1
fi

# RUN 2: Should NOT calculate CRC (cached)
run_test_pass 2
if grep -q "Calculating CRC32" "$TEST_ROOT/pass2.log"; then
    echo "❌ Pass 2 ERROR: Recalculated CRC instead of using cache."
    exit 1
else
    echo "✅ Pass 2: Skipped calculation as expected (cached)."
fi

# Cleanup
rm -rf "$TEST_ROOT"
echo "--- ALL CACHE TESTS PASSED ---"
