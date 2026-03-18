# Calibre to Komga FUSE Mount Guide

This guide explains how to use the new FUSE-based virtual filesystem to expose your Calibre library to Komga without duplicating any files.

## 1. Prerequisites

### Python 3
Ensure you are using **Python 3.6 or higher**. On Ubuntu, use the `python3` command.

### Install fusepy
The script requires the `fusepy` library to interface with the Linux FUSE system.
```bash
pip install fusepy
```

### Install FUSE (System)
Ubuntu usually has this, but ensure it's present:
```bash
sudo apt update
sudo apt install libfuse2 fuse3
```

---

## 2. Basic Usage

To mount your Calibre library as a virtual Komga-compatible directory:

1. **Create an empty mount point:**
   ```bash
   mkdir -p ~/calibre2komga/komga_virtual
   ```

2. **Run the mount command:**
   ```bash
   python3 calibre2komga.py mount /path/to/calibre/library ~/calibre2komga/komga_virtual
   ```

3. **Verify:**
   Open a new terminal and check the contents of `~/calibre2komga/komga_virtual`. You should see your books organized by "Author - Series".

---

## 3. Permission for Docker & Multi-user

By default, a FUSE mount is only visible to the user who created it. To allow Docker (or other users) to access the files, you must enable the `allow_other` flag.

1. **Modify System FUSE Config:**
   Open `/etc/fuse.conf` and uncomment the `user_allow_other` line:
   ```bash
   sudo nano /etc/fuse.conf
   ```
   Find and change `#user_allow_other` to `user_allow_other`.

2. **Use the --allow-other flag:**
   Add the flag to your mount command:
   ```bash
   python3 calibre2komga.py mount /path/to/calibre/ ~/calibre2komga/komga_virtual --allow-other
   ```

---

## 4. Running in the Background

Since the mount command must stay running to keep the files visible, you have a few options for backgrounding:

### Option A: Quick Background (Terminal)
Run the command with `&` and redirect logs to a file:
```bash
python3 calibre2komga.py mount /path/to/calibre/ ~/calibre2komga/komga_virtual > calibre_mount.log 2>&1 &
```
*   **Check logs:** `tail -f calibre_mount.log`
*   **Stop it:** `fusermount -u ~/calibre2komga/komga_virtual`

### Option B: Systemd Service (Recommended)
This ensures the library mounts automatically on boot and restarts if it crashes.

1. **Create the service file:**
   ```bash
   sudo nano /etc/systemd/system/calibre2komga.service
   ```

2. **Paste this config** (Update paths and your username):
   ```ini
   [Unit]
   Description=Calibre to Komga FUSE Mount
   After=network.target

   [Service]
   Type=simple
   User=YOUR_USERNAME
   WorkingDirectory=/home/YOUR_USERNAME/calibre2komga
   ExecStart=/usr/bin/python3 calibre2komga.py mount /path/to/calibre/ /home/YOUR_USERNAME/calibre2komga/komga_virtual
   ExecStop=/usr/bin/fusermount -u /home/YOUR_USERNAME/calibre2komga/komga_virtual
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

3. **Enable and Start:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable calibre2komga
   sudo systemctl start calibre2komga
   ```

### Option C: Screen or Tmux
Use a terminal multiplexer if you want to be able to "pop in" and check the process manually.
```bash
screen -S calibre_mount
python3 calibre2komga.py mount ...
# Press Ctrl+A then D to detach
```

---

## 4. Unmounting
If you need to stop the virtual filesystem manually:
```bash
fusermount -u ~/calibre2komga/komga_virtual
```

---

## 5. Komga Configuration

### Native Linux
Point Komga's library path directly to the mount point (e.g., `/home/eyal/calibre2komga/komga_virtual`).

### Docker
If running Komga in Docker, you must bind-mount the **host mount point** into the container:
```yaml
services:
  komga:
    image: gotson/komga
    volumes:
      - /home/eyal/calibre2komga/komga_virtual:/books:ro
```
*(Note: Use the `:ro` flag for extra safety as the FUSE mount is already read-only.)*

## 6. Troubleshooting

### Error: "Transport endpoint is not connected"
If you see `OSError: [Errno 107] Transport endpoint is not connected`, it means a previous mount session didn't close cleanly, and the directory is stuck in a "ghost" state.

**Solution:** Force an unmount of the directory before trying again:
```bash
fusermount -u ~/calibre2komga/komga_virtual
```
If that fails, use the "lazy" unmount:
```bash
sudo umount -l ~/calibre2komga/komga_virtual
```

### Error: "fusermount: user has no write access to mountpoint"
Ensure the directory you are mounting to (the "mount point") is owned by your user or that you have write permissions to it.

---

## Important Notes
*   **Refresh:** The virtual filesystem reads the Calibre `metadata.db` **at startup**. If you add new books to Calibre, you must restart the script/service to see the new files in the mount.
*   **Permissions:** Ensure the user running the script has read access to your Calibre library and write access to the mount point directory.
