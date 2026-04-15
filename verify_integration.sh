#!/bin/bash
# Integration Test for calibre2komga Audiobook Injection

# Exit on error
set -e

# Setup paths
TEST_ROOT="integration_test_env"
MOCK_CALIBRE="$TEST_ROOT/mock_calibre"
MOCK_AUDIO="$TEST_ROOT/mock_audiobooks"
MOUNT_DIR="$TEST_ROOT/mock_mount"
VENV_DIR="venv"

echo "--- 1. Cleaning and Creating Test Environment ---"
rm -rf "$TEST_ROOT"
mkdir -p "$MOCK_CALIBRE/Author Name/Book Title (1)"
mkdir -p "$MOCK_AUDIO/my_audiobook"
mkdir -p "$MOUNT_DIR"

echo "--- 2. Creating Mock Files ---"
# Create mock audiobook file
echo "Audio Data" > "$MOCK_AUDIO/my_audiobook/audio1.mp3"
# Create mock EPUB (standard ZIP)
python3 -c "import zipfile; z = zipfile.ZipFile('$MOCK_CALIBRE/Author Name/Book Title (1)/Book Title.epub', 'w'); z.writestr('mimetype', 'application/epub+zip'); z.close()"

echo "--- 3. Creating Mock Calibre Database ---"
python3 <<EOF
import sqlite3
conn = sqlite3.connect('$MOCK_CALIBRE/metadata.db')
c = conn.cursor()

# Calibre Schema
c.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, path TEXT, series_index REAL DEFAULT 1.0)")
c.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)")
c.execute("CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER)")
c.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT)")
c.execute("CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT)")
c.execute("CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT)")
c.execute("CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER)")

# Add Audiobook Custom Column
c.execute("INSERT INTO custom_columns (id, label, name, datatype) VALUES (1, 'audiobook_path', 'Audiobook Path', 'text')")
c.execute("CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, book INTEGER, value TEXT)")

# Insert Mock Book Data
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

echo "--- 4. Starting Virtual FUSE Mount ---"
if [ ! -d "$VENV_DIR" ]; then
    echo "Error: venv not found. Please run 'python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt' first."
    exit 1
fi

source "$VENV_DIR/bin/activate"
python3 calibre2komga.py mount "$MOCK_CALIBRE" "$MOUNT_DIR" \
  --audiobook-column "audiobook_path" \
  --audiobook-base-path "$MOCK_AUDIO" &
MOUNT_PID=$!

# Ensure we cleanup on exit even if something fails
trap "echo '--- Cleaning up... ---'; fusermount -u '$MOUNT_DIR' || true; kill $MOUNT_PID || true; rm -rf '$TEST_ROOT'" EXIT

echo "Waiting for mount to initialize..."
sleep 3

echo "--- 5. Verifying Results ---"
VIRTUAL_FILE="$MOUNT_DIR/Test Series/Volume 01 - Book Title.epub"

if [ ! -f "$VIRTUAL_FILE" ]; then
    echo "❌ ERROR: Virtual EPUB not found at $VIRTUAL_FILE"
    exit 1
fi

echo "✅ Virtual File Found. Checking contents..."
# List contents
unzip -l "$VIRTUAL_FILE"

# Check for audiobook folder
if unzip -l "$VIRTUAL_FILE" | grep -q "audiobook/audio1.mp3"; then
    echo "✅ SUCCESS: Audiobook file found inside virtual EPUB."
else
    echo "❌ ERROR: Audiobook file NOT found inside virtual EPUB."
    exit 1
fi

# Verify ZIP integrity
echo "Verifying ZIP integrity..."
unzip -t "$VIRTUAL_FILE"

echo "--- TEST PASSED SUCCESSFULLY ---"
