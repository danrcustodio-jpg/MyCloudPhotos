# My Cloud Photos

Local photo organizer for **WD My Cloud Home**. Browse, deduplicate, import, organize, and **archive older iCloud Photos** onto your drive over SMB — without relying on [home.mycloud.com](https://home.mycloud.com/cloud/file).

## Prerequisites

1. On the same Wi‑Fi as your My Cloud Home, open the device dashboard (connection type must be **Local**).
2. Enable **[Local Network Access](https://support-en.wd.com/app/answers/detailweb/a_id/34991)** and set a username/password.
3. Map the share in Windows File Explorer (e.g. `Z:` → `\\MYCLOUD-XXXX\YourShare`).

## Setup

```powershell
cd C:\Users\Owner\MyCloudPhotos
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` (or use **Settings** in the UI) and set:

```
PHOTO_ROOT=Z:\Photos
```

Use your real mapped path or a UNC path like `\\MYCLOUD-XXXX\User\Photos`.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python run.py
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787).

1. Confirm **Settings → PHOTO_ROOT**
2. Click **Scan library** (first scan hashes every image; later scans are incremental)
3. Use **Browse**, **Duplicates**, **Import**, **Archive**, and **Organize**

## Features

| Area | What it does |
|------|----------------|
| **Browse** | Folder + date filters, thumbnail grid, lightbox, smart folders |
| **Map** | Photos with GPS EXIF on an interactive map |
| **Duplicates** | Exact (SHA-256) and near (perceptual hash) groups; quarantine or delete |
| **Import** | Preview/copy from allowed folders (PHOTO_ROOT, Downloads/Desktop/Pictures/…, or `IMPORT_ALLOW_PREFIXES`) |
| **Archive** | Rolling iCloud archive per family Apple ID; keep recent window on iCloud |
| **Organize** | Move into `YYYY/YYYY-MM` (or other patterns); move selected into a folder |
| **People** | On-device face scan, cluster, and name people |
| **File Converter** | RAW / misnamed camera files → JPEG |
| **Settings** | PHOTO_ROOT, scan workers, junk/corrupt cleanup |

Thumbnails and the SQLite index live in `./data` on your PC — originals stay on My Cloud.

## iCloud rolling archive

Keep recent photos on iCloud for easy phone access (for example the last **2 years**). Archive older originals to the WD so iCloud stays manageable.

### Workflow

1. Open **Archive**, select a family profile, and sign in with that person's Apple ID (2FA when prompted — use **Resend 2FA** if no code arrives).
2. Click **Refresh inventory** (first run can take a while — it caches dates/sizes locally; progress shows %).
3. Set **Keep on iCloud** (years or months), or a date range, or pick assets in the grid.
4. Click **Estimate space** to see how many assets / GB would leave iCloud.
5. First pass: leave **Delete from iCloud** unchecked → **Archive to WD** (download + dedupe import only).
6. Confirm files appear under `FromiCloud/<Name>/…` on the drive (and in Browse after import).
7. Re-run with **Delete from iCloud after verified on WD** checked when you are ready to free cloud space.

Deletions go to iCloud **Recently Deleted** (~30 days), not permanent erase.

### Safety notes

- Estimate never deletes anything.
- Delete from iCloud requires typing **`DELETE FROM ICLOUD`** in the UI (plus `confirm_delete`).
- Permanent quarantine deletes require **`DELETE FOREVER`**; junk/corrupt cleanup requires **`DELETE FILES`**.
- Assets are only removed from iCloud after a verified copy exists under `PHOTO_ROOT`.
- Exact (and optional near) duplicates already on the WD are skipped.
- Auth cookies live under `data/icloud/` on this PC — do not commit that folder. Password may be stored in the OS keyring.
- This uses the same unofficial Photos API family as [icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader) (`pyicloud`). Apple can change behavior; treat delete carefully.
- Family accounts (Dan / Jess / Graham / Lillian by default) are separate profiles with separate cookies under `data/icloud/cookies/a{id}/`. Add / rename / remove profiles on the Archive tab.

### Batching

Archives run in batches (default **500** assets; set `ICLOUD_ARCHIVE_BATCH_SIZE` in `.env`). Progress shows phase (`download` → `import` → `verify` → `remove_from_icloud`), batch index, and bytes.

## Notes

- HEIC may need extra codecs on Windows; JPEG/PNG/WebP/TIFF work out of the box.
- Near-duplicate search is O(n²) over unique hashes — fine for tens of thousands; full libraries may take a minute.
- Quarantined duplicates go to `_quarantine_duplicates` under your photo root.
- Remote access while traveling is still via WD’s site (or add Tailscale later). This app is for home-network use.
- Avoid running **library scan** and **iCloud inventory/archive** at the same time — they share one SQLite DB. The app will refuse overlapping starts.
- A banner appears while a heavy job runs; Browse/Map/estimates stay available, Start actions for other jobs are paused.
- Offline tests (mocked iCloud / temp PHOTO_ROOT, no live Apple or WD): `python -m pytest tests -v`
- Family profiles (Dan / Jess / Graham / Lillian) are seeded on Archive — each uses a separate Apple ID and cookie folder under `data/icloud/cookies/`.
