# Virtual Audiobook Embedding - Requirements

## 1. Core Goal
Seamlessly present audiobook files as if they are bundled *inside* the corresponding eBook's `.epub` file within the Komga virtual filesystem, without altering any physical files on the disk.

## 2. Presentation (The Virtual ZIP)
*   When an application (like Komga) accesses an `.epub` file through the virtual FUSE mount, it will read a dynamically generated ZIP archive stream.
*   This virtual ZIP will contain:
    *   All the original contents of the physical `.epub` file.
    *   A new directory (e.g., `audiobook/`) injected into the root of the ZIP structure.
    *   The associated audio files located inside this virtual `audiobook/` directory.

## 3. Data Source (Calibre DB Integration)
*   The script will query a specific **custom column** in the Calibre SQLite database (`metadata.db`) to find the location of the audio files for a given book.
*   The path stored in the database can be relative.

## 4. Configurable Parameters
The script will expose the following new configuration options (e.g., via CLI arguments):
1.  **Custom Column Name:** The name of the custom column in Calibre to query for the audiobook path (e.g., `#audiobookfiles`).
2.  **Audiobook Folder Base Path:** A base directory path. If the path stored in the Calibre custom column is relative, it will be joined with this base path to resolve the absolute location of the audio files.
3.  **Allowed Audio Extensions:** A configurable list of file extensions (e.g., `.mp3`, `.m4a`, `.m4b`). When scanning the resolved audiobook folder, only files matching these extensions will be included in the virtual ZIP.

## 5. Non-Destructive Operation
*   The original `.epub` files will not be modified, repackaged, or copied.
*   The audio files will not be moved, modified, or copied.
*   The entire merging process happens "on-the-fly" in memory when the FUSE driver handles file reads.

## 6. Performance & Compatibility
*   Metadata scanning tools must remain fast. The FUSE driver will instantly serve the generated ZIP headers and indices (Central Directory, EOCD) from memory.
*   File reads for actual content (either original EPUB data or appended audio data) will be redirected directly to the respective physical files on disk.

## 7. User Expectations
1.  **Calibre Configuration:** Create a custom text column in Calibre to store the audiobook paths.
2.  **Data Entry:** Populate this custom column with the valid relative or absolute path to the folder containing the audio files for the relevant books.
3.  **Script Execution:** Provide the necessary configuration parameters (column name, base path, extensions) when running the script.
