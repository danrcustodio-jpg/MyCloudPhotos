from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import DB_PATH, ensure_data_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    folder TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    width INTEGER,
    height INTEGER,
    exif_date TEXT,
    latitude REAL,
    longitude REAL,
    media_type TEXT DEFAULT 'image',
    duration REAL,
    content_hash TEXT,
    phash TEXT,
    thumb_path TEXT,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_exif_date ON photos(exif_date);
CREATE INDEX IF NOT EXISTS idx_photos_content_hash ON photos(content_hash);
CREATE INDEX IF NOT EXISTS idx_photos_phash ON photos(phash);
CREATE INDEX IF NOT EXISTS idx_photos_mtime ON photos(mtime);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    removed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_dir TEXT NOT NULL,
    dest_folder TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    copied INTEGER NOT NULL DEFAULT 0,
    skipped_exact INTEGER NOT NULL DEFAULT 0,
    skipped_near INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quarantine_path TEXT NOT NULL UNIQUE,
    original_path TEXT,
    original_rel_path TEXT,
    filename TEXT NOT NULL,
    size INTEGER,
    content_hash TEXT,
    thumb_path TEXT,
    quarantined_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS icloud_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    apple_id TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    inventory_refreshed_at TEXT,
    inventory_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS icloud_assets (
    account_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    filename TEXT,
    created_at TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    item_type TEXT NOT NULL DEFAULT 'image',
    is_live_photo INTEGER NOT NULL DEFAULT 0,
    library_key TEXT NOT NULL DEFAULT 'root',
    archived_locally INTEGER NOT NULL DEFAULT 0,
    deleted_from_icloud INTEGER NOT NULL DEFAULT 0,
    local_rel_path TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (account_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_icloud_assets_created ON icloud_assets(created_at);
CREATE INDEX IF NOT EXISTS idx_icloud_assets_size ON icloud_assets(size);
CREATE INDEX IF NOT EXISTS idx_icloud_assets_type ON icloud_assets(item_type);

CREATE TABLE IF NOT EXISTS icloud_archive_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    status TEXT NOT NULL,
    phase TEXT,
    mode TEXT,
    cutoff TEXT,
    range_start TEXT,
    range_end TEXT,
    delete_after INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    skipped_exact INTEGER NOT NULL DEFAULT 0,
    skipped_near INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    deleted_icloud INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    batch_index INTEGER NOT NULL DEFAULT 0,
    batch_count INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    cover_face_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id INTEGER NOT NULL,
    person_id INTEGER,
    cluster_key TEXT,
    x REAL NOT NULL,
    y REAL NOT NULL,
    w REAL NOT NULL,
    h REAL NOT NULL,
    det_score REAL,
    embedding BLOB NOT NULL,
    thumb_path TEXT,
    ignored INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE,
    FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_key);
CREATE INDEX IF NOT EXISTS idx_faces_ignored ON faces(ignored);

CREATE TABLE IF NOT EXISTS face_clusters (
    cluster_key TEXT PRIMARY KEY,
    face_count INTEGER NOT NULL DEFAULT 0,
    photo_count INTEGER NOT NULL DEFAULT 0,
    best_score REAL,
    cover_face_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_face_clusters_count ON face_clusters(face_count DESC);
"""

_PHOTO_COLUMN_MIGRATIONS = (
    ("latitude", "REAL"),
    ("longitude", "REAL"),
    ("media_type", "TEXT DEFAULT 'image'"),
    ("duration", "REAL"),
    ("face_scanned_at", "TEXT"),
)


SETTING_APPLE_ID_LEGACY = "icloud_apple_id"
SETTING_ACTIVE_ACCOUNT = "icloud_active_account_id"

# Family Shared iCloud archive profiles (each person has their own Apple ID)
_FAMILY_ACCOUNTS = (
    ("Dan", 0),
    ("Jess", 1),
    ("Graham", 2),
    ("Lillian", 3),
)


def _migrate_photos_columns(conn: sqlite3.Connection) -> None:
    """Add GPS / media / faces hooks if missing (safe for existing DBs)."""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(photos)").fetchall()
    }
    for col, col_type in _PHOTO_COLUMN_MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {col_type}")


def _seed_icloud_accounts(conn: sqlite3.Connection) -> None:
    for label, sort_order in _FAMILY_ACCOUNTS:
        conn.execute(
            """
            INSERT INTO icloud_accounts(label, sort_order)
            VALUES (?, ?)
            ON CONFLICT(label) DO UPDATE SET sort_order = excluded.sort_order
            """,
            (label, sort_order),
        )
    # Migrate legacy single Apple ID setting onto Jess's profile
    legacy = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTING_APPLE_ID_LEGACY,)
    ).fetchone()
    if legacy and legacy["value"]:
        jess = conn.execute(
            "SELECT id, apple_id FROM icloud_accounts WHERE label = 'Jess'"
        ).fetchone()
        if jess and not jess["apple_id"]:
            conn.execute(
                "UPDATE icloud_accounts SET apple_id = ?, updated_at = datetime('now') WHERE id = ?",
                (legacy["value"].strip(), jess["id"]),
            )
    # Ensure an active account is selected
    active = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTING_ACTIVE_ACCOUNT,)
    ).fetchone()
    if not active:
        # Prefer Jess if she has an Apple ID, else first by sort_order
        preferred = conn.execute(
            """
            SELECT id FROM icloud_accounts
            WHERE apple_id IS NOT NULL AND trim(apple_id) != ''
            ORDER BY CASE WHEN label = 'Jess' THEN 0 ELSE 1 END, sort_order
            LIMIT 1
            """
        ).fetchone()
        if not preferred:
            preferred = conn.execute(
                "SELECT id FROM icloud_accounts ORDER BY sort_order, id LIMIT 1"
            ).fetchone()
        if preferred:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                (SETTING_ACTIVE_ACCOUNT, str(preferred["id"])),
            )


def _migrate_icloud_assets_table(conn: sqlite3.Connection) -> None:
    """Upgrade legacy asset-id-only inventory to per-account composite keys."""
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "icloud_assets" not in tables:
        return

    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(icloud_assets)").fetchall()
    }
    if "account_id" in cols:
        if "library_key" not in cols:
            conn.execute(
                "ALTER TABLE icloud_assets ADD COLUMN library_key TEXT NOT NULL DEFAULT 'root'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_icloud_assets_account ON icloud_assets(account_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_icloud_assets_library ON icloud_assets(library_key)"
        )
        return

    # Legacy table: rebuild with account_id (assign existing rows to Jess / active)
    jess = conn.execute(
        "SELECT id FROM icloud_accounts WHERE label = 'Jess'"
    ).fetchone()
    account_id = int(jess["id"]) if jess else 1
    has_library = "library_key" in cols
    conn.execute(
        """
        CREATE TABLE icloud_assets_v2 (
            account_id INTEGER NOT NULL,
            asset_id TEXT NOT NULL,
            filename TEXT,
            created_at TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            item_type TEXT NOT NULL DEFAULT 'image',
            is_live_photo INTEGER NOT NULL DEFAULT 0,
            library_key TEXT NOT NULL DEFAULT 'root',
            archived_locally INTEGER NOT NULL DEFAULT 0,
            deleted_from_icloud INTEGER NOT NULL DEFAULT 0,
            local_rel_path TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (account_id, asset_id)
        )
        """
    )
    if has_library:
        conn.execute(
            f"""
            INSERT INTO icloud_assets_v2(
                account_id, asset_id, filename, created_at, size, item_type,
                is_live_photo, library_key, archived_locally, deleted_from_icloud,
                local_rel_path, updated_at
            )
            SELECT ?, asset_id, filename, created_at, size, item_type,
                   is_live_photo, COALESCE(library_key, 'root'), archived_locally,
                   deleted_from_icloud, local_rel_path, updated_at
            FROM icloud_assets
            """,
            (account_id,),
        )
    else:
        conn.execute(
            """
            INSERT INTO icloud_assets_v2(
                account_id, asset_id, filename, created_at, size, item_type,
                is_live_photo, library_key, archived_locally, deleted_from_icloud,
                local_rel_path, updated_at
            )
            SELECT ?, asset_id, filename, created_at, size, item_type,
                   is_live_photo, 'root', archived_locally,
                   deleted_from_icloud, local_rel_path, updated_at
            FROM icloud_assets
            """,
            (account_id,),
        )
    conn.execute("DROP TABLE icloud_assets")
    conn.execute("ALTER TABLE icloud_assets_v2 RENAME TO icloud_assets")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_icloud_assets_created ON icloud_assets(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_icloud_assets_size ON icloud_assets(size)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_icloud_assets_type ON icloud_assets(item_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_icloud_assets_account ON icloud_assets(account_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_icloud_assets_library ON icloud_assets(library_key)"
    )
    # Carry inventory timestamps onto Jess's account row
    inv_at = conn.execute(
        "SELECT value FROM settings WHERE key = 'icloud_inventory_refreshed_at'"
    ).fetchone()
    inv_count = conn.execute(
        "SELECT value FROM settings WHERE key = 'icloud_inventory_count'"
    ).fetchone()
    if inv_at or inv_count:
        conn.execute(
            """
            UPDATE icloud_accounts
            SET inventory_refreshed_at = COALESCE(?, inventory_refreshed_at),
                inventory_count = COALESCE(?, inventory_count),
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                inv_at["value"] if inv_at else None,
                int(inv_count["value"]) if inv_count and inv_count["value"] else 0,
                account_id,
            ),
        )


def _migrate_icloud_archive_jobs(conn: sqlite3.Connection) -> None:
    """Add account_id to archive jobs if missing (existing DBs)."""
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "icloud_archive_jobs" not in tables:
        return
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(icloud_archive_jobs)").fetchall()
    }
    if "account_id" not in cols:
        conn.execute(
            "ALTER TABLE icloud_archive_jobs ADD COLUMN account_id INTEGER"
        )


def init_db() -> None:
    ensure_data_dirs()
    with connect() as conn:
        # Accounts first (needed before asset migration)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS icloud_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE,
                apple_id TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                inventory_refreshed_at TEXT,
                inventory_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.executescript(SCHEMA)
        _migrate_photos_columns(conn)
        _seed_icloud_accounts(conn)
        _migrate_icloud_assets_table(conn)
        _migrate_icloud_archive_jobs(conn)
        # Safe to create after account_id exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_icloud_assets_account ON icloud_assets(account_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_icloud_assets_library ON icloud_assets(library_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_media_type ON photos(media_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_face_scanned ON photos(face_scanned_at)"
        )


def open_connection() -> sqlite3.Connection:
    """
    Open a SQLite connection with WAL + busy timeout.
    Prefer `connect()` for short transactional work; use this for long-lived writers
    (e.g. library scan) that manage their own commits.
    """
    ensure_data_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = open_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_with_retry(fn, *, attempts: int = 8, label: str = "db"):
    """Retry on sqlite 'database is locked' / busy errors."""
    import time

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise last or RuntimeError(f"{label} failed after retries")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def strip_photo_path(photo: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove absolute filesystem path from API-facing photo dicts."""
    if photo is None:
        return None
    out = dict(photo)
    out.pop("path", None)
    return out


def strip_photo_paths(photos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [strip_photo_path(p) or {} for p in photos]
