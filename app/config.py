from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _is_drive_letter_path(path: Path | str) -> bool:
    s = str(path)
    return len(s) >= 2 and s[1] == ":" and s[0].isalpha()


def normalize_fs_path(value: str | Path, *, must_exist: bool = False) -> Path:
    """
    Normalize a filesystem path.

    Important: on Windows, Path.resolve() turns mapped drives (Z:\\) into UNC
    paths (\\\\server\\share). That can hang or fail for long SMB walks, so we
    keep drive-letter paths as drive letters.
    """
    path = Path(str(value).strip().strip('"')).expanduser()
    if _is_drive_letter_path(path):
        # Absolute drive path — do not resolve() to UNC
        normalized = Path(os.path.abspath(str(path)))
    else:
        normalized = path.resolve()
    if must_exist and not (normalized.exists() and normalized.is_dir()):
        raise ValueError(f"Path is not a directory: {normalized}")
    return normalized


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    try:
        return normalize_fs_path(value)
    except OSError:
        return Path(value).expanduser()


PHOTO_ROOT = _path(os.getenv("PHOTO_ROOT"), Path())
DATA_DIR = _path(os.getenv("DATA_DIR"), _ROOT / "data")
DB_PATH = DATA_DIR / "index.db"
THUMBS_DIR = DATA_DIR / "thumbs"
ICLOUD_DIR = DATA_DIR / "icloud"
ICLOUD_COOKIE_DIR = ICLOUD_DIR / "cookies"
ICLOUD_STAGING_DIR = DATA_DIR / "icloud_staging"
ICLOUD_THUMBS_DIR = DATA_DIR / "icloud_thumbs"
QUARANTINE_DIR_NAME = "_quarantine_duplicates"
FACES_DIR = DATA_DIR / "faces"
INSIGHTFACE_ROOT = DATA_DIR / "insightface"

# Archive batch size (download → import → verify → optional delete)
ICLOUD_ARCHIVE_BATCH_SIZE = max(1, int(os.getenv("ICLOUD_ARCHIVE_BATCH_SIZE", "500")))

THUMB_SIZE = int(os.getenv("THUMB_SIZE", "320"))
PHASH_THRESHOLD = int(os.getenv("PHASH_THRESHOLD", "8"))
SCAN_WORKERS = max(1, int(os.getenv("SCAN_WORKERS", "3")))
_SCAN_WORKERS_MIN = 1
_SCAN_WORKERS_MAX = 8

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".bmp",
}

# Camera RAW — converted to JPEG for archive; not indexed as-is.
RAW_EXTENSIONS = {
    ".nef",
    ".nrw",
    ".cr2",
    ".cr3",
    ".arw",
    ".dng",
    ".raf",
    ".orf",
    ".rw2",
}

QUARANTINE_RAW_DIR_NAME = "_quarantine_raw"

# Hooks for a future video indexer / Google Photos-style UI.
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".3gp",
}


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    ICLOUD_DIR.mkdir(parents=True, exist_ok=True)
    ICLOUD_COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    ICLOUD_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    ICLOUD_THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    INSIGHTFACE_ROOT.mkdir(parents=True, exist_ok=True)


def photo_root_configured() -> bool:
    root = os.getenv("PHOTO_ROOT", "").strip()
    if not root:
        return False
    path = Path(root).expanduser()
    try:
        return path.exists() and path.is_dir()
    except OSError:
        return False


def get_photo_root() -> Path:
    root = os.getenv("PHOTO_ROOT", "").strip()
    if not root:
        raise ValueError("PHOTO_ROOT is not set. Copy .env.example to .env and set your My Cloud path.")
    path = normalize_fs_path(root)
    if not path.exists():
        raise ValueError(f"PHOTO_ROOT does not exist: {path}")
    return path


def _set_env_value(key: str, value: str) -> None:
    """Persist a KEY=value line in .env and update process env."""
    env_path = _ROOT / ".env"
    lines: list[str] = []
    found = False
    prefix = f"{key}="
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(prefix):
                lines.append(f"{prefix}{value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{prefix}{value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def set_photo_root(path: str) -> Path:
    """Persist PHOTO_ROOT to .env and update process env."""
    resolved = normalize_fs_path(path, must_exist=True)
    _set_env_value("PHOTO_ROOT", str(resolved))
    global PHOTO_ROOT
    PHOTO_ROOT = resolved
    return resolved


def get_scan_workers() -> int:
    return SCAN_WORKERS


def set_scan_workers(workers: int) -> int:
    """Persist SCAN_WORKERS to .env (applies to the next scan start)."""
    try:
        n = int(workers)
    except (TypeError, ValueError) as e:
        raise ValueError("SCAN_WORKERS must be an integer") from e
    if n < _SCAN_WORKERS_MIN or n > _SCAN_WORKERS_MAX:
        raise ValueError(
            f"SCAN_WORKERS must be between {_SCAN_WORKERS_MIN} and {_SCAN_WORKERS_MAX}"
        )
    _set_env_value("SCAN_WORKERS", str(n))
    global SCAN_WORKERS
    SCAN_WORKERS = n
    return n
