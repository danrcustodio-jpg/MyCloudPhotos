from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app.config import QUARANTINE_DIR_NAME, get_photo_root
from app.database import connect, rows_to_dicts
from app.paths import assert_under_root
from app.services.hashing import is_media


def quarantine_dir() -> Path:
    return assert_under_root(get_photo_root() / QUARANTINE_DIR_NAME, get_photo_root())


def ensure_quarantine_dir() -> Path:
    d = quarantine_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_quarantine(
    *,
    quarantine_path: Path,
    original_path: str | None,
    original_rel_path: str | None,
    filename: str,
    size: int | None,
    content_hash: str | None,
    thumb_path: str | None,
) -> int:
    """Insert a quarantine row; returns new id."""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO quarantine(
              quarantine_path, original_path, original_rel_path, filename,
              size, content_hash, thumb_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(quarantine_path) DO UPDATE SET
              original_path=excluded.original_path,
              original_rel_path=excluded.original_rel_path,
              filename=excluded.filename,
              size=excluded.size,
              content_hash=excluded.content_hash,
              thumb_path=COALESCE(excluded.thumb_path, quarantine.thumb_path)
            """,
            (
                str(quarantine_path),
                original_path,
                original_rel_path,
                filename,
                size,
                content_hash,
                thumb_path,
            ),
        )
        row = conn.execute(
            "SELECT id FROM quarantine WHERE quarantine_path = ?",
            (str(quarantine_path),),
        ).fetchone()
        return int(row["id"] if row else cur.lastrowid)


def sync_disk_into_db() -> int:
    """Index files already in the quarantine folder that are not in the DB."""
    try:
        qdir = ensure_quarantine_dir()
    except ValueError:
        return 0

    added = 0
    with connect() as conn:
        known = {
            r["quarantine_path"]
            for r in conn.execute("SELECT quarantine_path FROM quarantine").fetchall()
        }
        for row in list(
            conn.execute("SELECT id, quarantine_path FROM quarantine").fetchall()
        ):
            if not Path(row["quarantine_path"]).exists():
                conn.execute("DELETE FROM quarantine WHERE id = ?", (row["id"],))
                known.discard(row["quarantine_path"])

    for entry in qdir.iterdir():
        if not entry.is_file() or not is_media(entry):
            continue
        path_str = str(entry)
        if path_str in known:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        record_quarantine(
            quarantine_path=entry,
            original_path=None,
            original_rel_path=None,
            filename=entry.name,
            size=size,
            content_hash=None,
            thumb_path=None,
        )
        known.add(path_str)
        added += 1
    return added


def quarantine_count() -> int:
    try:
        sync_disk_into_db()
    except Exception:
        pass
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"])


def list_quarantine(limit: int = 120, offset: int = 0) -> dict[str, Any]:
    sync_disk_into_db()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    with connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"])
        rows = conn.execute(
            """
            SELECT id, quarantine_path, original_path, original_rel_path, filename,
                   size, content_hash, thumb_path, quarantined_at
            FROM quarantine
            ORDER BY quarantined_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    items = []
    for r in rows_to_dicts(rows):
        items.append(
            {
                "id": r["id"],
                "filename": r["filename"],
                "rel_path": r["original_rel_path"]
                or f"{QUARANTINE_DIR_NAME}/{r['filename']}",
                "original_rel_path": r["original_rel_path"],
                "folder": QUARANTINE_DIR_NAME,
                "size": r["size"] or 0,
                "content_hash": r["content_hash"],
                "quarantined_at": r["quarantined_at"],
                "media_type": "image",
                "in_quarantine": True,
            }
        )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
        "quarantine_dir": str(quarantine_dir()),
    }


def get_quarantine_item(item_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM quarantine WHERE id = ?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_quarantine_items(
    ids: list[int], *, confirm_token: str | None = None
) -> dict[str, Any]:
    """Permanently delete quarantined files from disk and index."""
    from app.services.safety import CONFIRM_DELETE_FOREVER, require_confirm_token

    require_confirm_token(
        confirm_token,
        CONFIRM_DELETE_FOREVER,
        action="permanently delete quarantined files",
    )
    results: dict[str, Any] = {"deleted": [], "errors": []}
    root = get_photo_root()
    with connect() as conn:
        for iid in ids:
            row = conn.execute(
                "SELECT * FROM quarantine WHERE id = ?", (iid,)
            ).fetchone()
            if not row:
                results["errors"].append({"id": iid, "error": "not found"})
                continue
            try:
                path = assert_under_root(Path(row["quarantine_path"]), root)
                if QUARANTINE_DIR_NAME not in path.parts:
                    raise ValueError("Not under quarantine folder")
                path.unlink(missing_ok=True)
                thumb = row["thumb_path"]
                if thumb:
                    Path(thumb).unlink(missing_ok=True)
                conn.execute("DELETE FROM quarantine WHERE id = ?", (iid,))
                results["deleted"].append(iid)
            except (OSError, ValueError) as e:
                results["errors"].append({"id": iid, "error": str(e)})
    results["deleted_count"] = len(results["deleted"])
    return results


def restore_quarantine_items(ids: list[int]) -> dict[str, Any]:
    """
    Move files out of quarantine.
    Prefer original_path when known and parent exists; else restore to Inbox/.
    Does not re-index into photos — run a scan/resume afterward to see them in Browse.
    """
    results: dict[str, Any] = {"restored": [], "errors": []}
    root = get_photo_root()
    with connect() as conn:
        for iid in ids:
            row = conn.execute(
                "SELECT * FROM quarantine WHERE id = ?", (iid,)
            ).fetchone()
            if not row:
                results["errors"].append({"id": iid, "error": "not found"})
                continue
            try:
                src = assert_under_root(Path(row["quarantine_path"]), root)
                if not src.exists():
                    conn.execute("DELETE FROM quarantine WHERE id = ?", (iid,))
                    results["errors"].append({"id": iid, "error": "file missing"})
                    continue

                dest: Path | None = None
                if row["original_path"]:
                    try:
                        candidate = assert_under_root(Path(row["original_path"]), root)
                        if QUARANTINE_DIR_NAME not in candidate.parts:
                            candidate.parent.mkdir(parents=True, exist_ok=True)
                            dest = candidate
                    except ValueError:
                        dest = None
                if dest is None:
                    inbox = assert_under_root(root / "Inbox", root)
                    inbox.mkdir(parents=True, exist_ok=True)
                    dest = inbox / (row["filename"] or src.name)

                if dest.exists():
                    stem, suffix = dest.stem, dest.suffix
                    n = 1
                    while dest.exists():
                        dest = dest.parent / f"{stem}_restored_{n}{suffix}"
                        n += 1

                shutil.move(str(src), str(dest))
                thumb = row["thumb_path"]
                if thumb:
                    Path(thumb).unlink(missing_ok=True)
                conn.execute("DELETE FROM quarantine WHERE id = ?", (iid,))
                results["restored"].append(
                    {"id": iid, "path": str(dest), "rel_path": str(dest.relative_to(root))}
                )
            except (OSError, ValueError) as e:
                results["errors"].append({"id": iid, "error": str(e)})
    results["restored_count"] = len(results["restored"])
    return results
