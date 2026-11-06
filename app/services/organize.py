"""Move / rename photos on the WD drive and update the SQLite index."""

from __future__ import annotations

import os
import shutil
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_photo_root
from app.database import connect
from app.paths import assert_under_root

_UNDATED_DEFAULT = "Undated"
_PROTECTED_DIR_NAMES = {
    ".trash",
    "#recycle",
    "@eadir",
    "system volume information",
}


def _path_exists(path: Path) -> bool:
    """exists() that treats permission errors as 'present but inaccessible'."""
    try:
        return path.exists()
    except OSError:
        return True


def _unique_dest(dest: Path) -> Path:
    if not _path_exists(dest):
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        candidate = dest.with_name(f"{stem}_{n}{suffix}")
        if not _path_exists(candidate):
            return candidate
        n += 1


@dataclass
class OrganizeState:
    status: str = "idle"
    phase: str = ""
    total: int = 0
    processed: int = 0
    moved: int = 0
    skipped: int = 0
    missing: int = 0
    errors: int = 0
    folders_removed: int = 0
    message: str = ""
    report: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    pause_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = OrganizeState()


def get_organize_status() -> dict[str, Any]:
    with _state.lock:
        paused = bool(_state.pause_requested) and _state.status == "running"
        return {
            "status": _state.status,
            "phase": "paused" if paused else _state.phase,
            "total": _state.total,
            "processed": _state.processed,
            "moved": _state.moved,
            "skipped": _state.skipped,
            "missing": _state.missing,
            "errors": _state.errors,
            "moved_count": _state.moved,
            "skipped_count": _state.skipped,
            "missing_count": _state.missing,
            "error_count": _state.errors,
            "folders_removed": _state.folders_removed,
            "message": _state.message,
            "cancel_requested": _state.cancel_requested,
            "paused": paused,
            "report": list(_state.report[-200:]),
        }


def request_organize_cancel() -> dict[str, Any]:
    with _state.lock:
        if _state.status == "running":
            _state.cancel_requested = True
            _state.pause_requested = False
            _state.phase = "cancelling"
            _state.message = "Cancelling… waiting for the current file check/move to finish"
        else:
            _state.cancel_requested = True
            _state.message = "Cancel requested…"
    return get_organize_status()


def request_organize_pause() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No organize job is running")
        if _state.cancel_requested:
            raise RuntimeError("Organize is cancelling")
        _state.pause_requested = True
        _state.phase = "paused"
        _state.message = "Paused — resume when ready (current file will finish)"
    return get_organize_status()


def request_organize_resume() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No organize job is running")
        _state.pause_requested = False
        if _state.phase == "paused":
            _state.phase = "moving"
        _state.message = "Resuming organize…"
    return get_organize_status()


def move_photos(
    photo_ids: list[int],
    dest_folder: str,
) -> dict[str, Any]:
    """Move selected photos into dest_folder (relative to photo root)."""
    from app.services.jobs import get_busy_status

    busy = get_busy_status()
    if busy.get("busy") and busy.get("active"):
        raise RuntimeError(
            f"Wait for {busy['active']['label']} to finish before moving photos"
        )
    root = get_photo_root()
    dest_folder = dest_folder.strip().replace("\\", "/").strip("/")
    dest_dir = assert_under_root(root / dest_folder if dest_folder else root, root)
    dest_dir.mkdir(parents=True, exist_ok=True)

    moved = []
    errors = []

    with connect() as conn:
        for pid in photo_ids:
            row = conn.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
            if not row:
                errors.append({"id": pid, "error": "not found"})
                continue
            try:
                src = assert_under_root(Path(row["path"]), root)
            except ValueError as e:
                errors.append({"id": pid, "error": str(e)})
                continue
            if not src.exists():
                errors.append({"id": pid, "error": "file missing on disk"})
                continue
            dest = _unique_dest(dest_dir / src.name)
            try:
                dest = assert_under_root(dest, root)
                shutil.move(str(src), str(dest))
                rel = dest.relative_to(root)
                folder = str(rel.parent).replace("\\", "/")
                if folder == ".":
                    folder = ""
                conn.execute(
                    """
                    UPDATE photos SET
                        path=?, rel_path=?, filename=?, folder=?,
                        indexed_at=datetime('now')
                    WHERE id=?
                    """,
                    (
                        str(dest),
                        str(rel).replace("\\", "/"),
                        dest.name,
                        folder,
                        pid,
                    ),
                )
                moved.append({"id": pid, "rel_path": str(rel).replace("\\", "/")})
            except Exception as e:
                errors.append({"id": pid, "error": str(e)})

    return {"moved": moved, "errors": errors}


def organize_by_date(
    photo_ids: list[int] | None = None,
    pattern: str = "YYYY/YYYY-MM",
    *,
    confirm: bool = False,
    consolidate: bool = False,
    undated_folder: str = _UNDATED_DEFAULT,
    remove_empty_folders: bool = False,
) -> dict[str, Any]:
    """
    Start a background job that moves photos into dated folders.
    pattern: 'YYYY/YYYY-MM' or 'YYYY-MM' or 'YYYY'
    If photo_ids is None, organize all photos that have a usable date.
    consolidate=True moves everything out of legacy folders (uses disk path, not
    just the DB folder field). Undated files go to undated_folder.
    Requires confirm=True when photo_ids is None (bulk library move).
    """
    if photo_ids is None and not confirm:
        raise ValueError("confirm=True is required when organizing the entire library")
    if pattern not in ("YYYY/YYYY-MM", "YYYY-MM", "YYYY"):
        raise ValueError("Invalid pattern")
    undated_folder = undated_folder.strip().replace("\\", "/").strip("/") or _UNDATED_DEFAULT

    from app.services.jobs import JOB_ORGANIZE, assert_can_start

    assert_can_start(JOB_ORGANIZE)

    with _state.lock:
        if _state.status == "running":
            raise RuntimeError("An organize job is already running")
        _state.status = "running"
        _state.phase = "starting"
        _state.total = 0
        _state.processed = 0
        _state.moved = 0
        _state.skipped = 0
        _state.missing = 0
        _state.errors = 0
        _state.folders_removed = 0
        _state.message = "Starting organize…"
        _state.report = []
        _state.cancel_requested = False
        _state.pause_requested = False

    thread = threading.Thread(
        target=_run_organize_by_date,
        kwargs={
            "photo_ids": photo_ids,
            "pattern": pattern,
            "consolidate": consolidate,
            "undated_folder": undated_folder,
            "remove_empty_folders": remove_empty_folders and consolidate,
        },
        daemon=True,
    )
    thread.start()
    return get_organize_status()


def _folder_for_date(dt: datetime, pattern: str) -> str:
    if pattern == "YYYY":
        return f"{dt.year:04d}"
    if pattern == "YYYY-MM":
        return f"{dt.year:04d}-{dt.month:02d}"
    return f"{dt.year:04d}/{dt.year:04d}-{dt.month:02d}"


def _date_for_row(row: dict[str, Any]) -> datetime | None:
    """Usable date = EXIF/camera date only (never file mtime / upload time)."""
    if row.get("exif_date"):
        try:
            return datetime.fromisoformat(row["exif_date"].replace(" ", "T"))
        except ValueError:
            pass
    return None


def _actual_folder(row: dict[str, Any], root: Path) -> str:
    """Folder relative to photo root where the file actually lives on disk."""
    try:
        src = assert_under_root(Path(row["path"]), root)
        rel = src.relative_to(root)
        parent = str(rel.parent).replace("\\", "/")
        return "" if parent == "." else parent
    except (ValueError, OSError):
        rel = (row.get("rel_path") or "").replace("\\", "/")
        if "/" not in rel:
            return ""
        return rel.rsplit("/", 1)[0]


def _should_skip_dir(name: str) -> bool:
    lower = name.lower()
    return name.startswith("_quarantine") or lower in _PROTECTED_DIR_NAMES


def _remove_empty_folders(root: Path) -> int:
    """Remove empty directories under root (deepest first). Keeps photo root."""
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root or _should_skip_dir(path.name):
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def _run_organize_by_date(
    *,
    photo_ids: list[int] | None,
    pattern: str,
    consolidate: bool,
    undated_folder: str,
    remove_empty_folders: bool,
) -> None:
    try:
        root = get_photo_root()
        with connect() as conn:
            if photo_ids:
                placeholders = ",".join("?" * len(photo_ids))
                rows = conn.execute(
                    f"SELECT * FROM photos WHERE id IN ({placeholders})",
                    photo_ids,
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM photos").fetchall()

        rows = [dict(r) for r in rows]
        with _state.lock:
            _state.total = len(rows)
            _state.phase = "moving"
            label = "Consolidating" if consolidate else "Organizing"
            _state.message = f"{label} {len(rows):,} photos…"

        batch: list[tuple] = []

        def _flush() -> None:
            nonlocal batch
            if not batch:
                return
            to_write = batch
            batch = []
            with connect() as conn:
                conn.executemany(
                    """
                    UPDATE photos SET
                        path=?, rel_path=?, filename=?, folder=?,
                        indexed_at=datetime('now')
                    WHERE id=?
                    """,
                    to_write,
                )

        def _cancelled() -> bool:
            with _state.lock:
                return _state.cancel_requested

        def _announce_pause() -> None:
            with _state.lock:
                _state.phase = "paused"
                _state.message = "Paused — click Resume when ready"

        for row in rows:
            from app.services.job_control import wait_while_paused

            if wait_while_paused(
                lock=_state.lock,
                is_cancelled=lambda: _state.cancel_requested,
                is_paused=lambda: _state.pause_requested,
                set_paused_ui=_announce_pause,
            ):
                _flush()
                _finish("cancelled", "Organize cancelled")
                return

            if _cancelled():
                _flush()
                _finish("cancelled", "Organize cancelled")
                return

            dt = _date_for_row(row)
            if dt is None:
                if not consolidate:
                    with _state.lock:
                        _state.skipped += 1
                        _state.processed += 1
                        _state.report.append(
                            {"id": row["id"], "action": "skip", "reason": "no usable date"}
                        )
                    continue
                folder = undated_folder
            else:
                folder = _folder_for_date(dt, pattern)

            actual = _actual_folder(row, root)
            if actual == folder:
                with _state.lock:
                    _state.skipped += 1
                    _state.processed += 1
                continue

            try:
                src = assert_under_root(Path(row["path"]), root)
            except ValueError as e:
                with _state.lock:
                    _state.errors += 1
                    _state.processed += 1
                    _state.report.append(
                        {"id": row["id"], "action": "error", "error": str(e)}
                    )
                continue

            if _cancelled():
                _flush()
                _finish("cancelled", "Organize cancelled")
                return
            try:
                exists = src.exists()
            except OSError as e:
                # SMB / Windows can raise Access Denied on exists() — don't abort the job
                with _state.lock:
                    _state.errors += 1
                    _state.processed += 1
                    _state.report.append(
                        {
                            "id": row["id"],
                            "action": "error",
                            "error": f"access denied / unreadable: {e}",
                            "rel_path": row.get("rel_path") or "",
                        }
                    )
                    _bump_message()
                continue
            if _cancelled():
                _flush()
                _finish("cancelled", "Organize cancelled")
                return

            if not exists:
                with _state.lock:
                    _state.missing += 1
                    _state.skipped += 1
                    _state.processed += 1
                    if _state.missing <= 20 or _state.missing % 100 == 0:
                        _state.report.append(
                            {
                                "id": row["id"],
                                "action": "skip",
                                "reason": "missing on disk",
                                "rel_path": row.get("rel_path") or "",
                            }
                        )
                    _bump_message()
                continue

            try:
                dest_dir = assert_under_root(root / folder, root)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique_dest(dest_dir / src.name)
                dest = assert_under_root(dest, root)
                if _cancelled():
                    _flush()
                    _finish("cancelled", "Organize cancelled")
                    return
                shutil.move(str(src), str(dest))
                rel = dest.relative_to(root)
                batch.append(
                    (
                        str(dest),
                        str(rel).replace("\\", "/"),
                        dest.name,
                        folder,
                        row["id"],
                    )
                )
                with _state.lock:
                    _state.moved += 1
                    _state.processed += 1
                    if _state.moved <= 50 or _state.moved % 25 == 0:
                        _state.report.append(
                            {
                                "id": row["id"],
                                "action": "moved",
                                "from": actual or "(root)",
                                "rel_path": str(rel).replace("\\", "/"),
                            }
                        )
                    _bump_message()
                if len(batch) >= 25:
                    _flush()
            except Exception as e:
                with _state.lock:
                    _state.errors += 1
                    _state.processed += 1
                    _state.report.append(
                        {
                            "id": row["id"],
                            "action": "error",
                            "error": str(e),
                            "rel_path": row.get("rel_path") or "",
                        }
                    )
                    _bump_message()

        _flush()

        if remove_empty_folders and not _cancelled():
            with _state.lock:
                _state.phase = "cleanup"
                _state.message = "Removing empty legacy folders…"
            removed = _remove_empty_folders(root)
            with _state.lock:
                _state.folders_removed = removed

        with _state.lock:
            msg = (
                f"Organize done. moved={_state.moved} skipped={_state.skipped} "
                f"(missing on disk={_state.missing}) errors={_state.errors}"
            )
            if _state.folders_removed:
                msg += f" empty_folders_removed={_state.folders_removed}"
        _finish("completed", msg)
    except Exception as e:
        _finish("error", f"{e}\n{traceback.format_exc()}")


def _bump_message() -> None:
    """Caller must hold _state.lock."""
    _state.message = (
        f"Moved {_state.moved:,} · skipped {_state.skipped:,}"
        f" (missing {_state.missing:,}) · errors {_state.errors:,} · "
        f"{_state.processed:,}/{_state.total:,}"
    )


def _finish(status: str, message: str) -> None:
    from app.services.jobs import JOB_ORGANIZE, release_job

    with _state.lock:
        _state.status = status
        _state.phase = status
        _state.message = message
    release_job(JOB_ORGANIZE)


def rename_photo(photo_id: int, new_filename: str) -> dict[str, Any]:
    new_filename = Path(new_filename).name  # prevent path traversal
    if not new_filename:
        raise ValueError("Filename required")

    root = get_photo_root()
    with connect() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            raise ValueError("Photo not found")
        src = assert_under_root(Path(row["path"]), root)
        dest = assert_under_root(src.with_name(new_filename), root)
        dest = _unique_dest(dest) if dest.exists() and dest != src else dest
        dest = assert_under_root(dest, root)
        shutil.move(str(src), str(dest))
        rel = dest.relative_to(root)
        folder = str(rel.parent).replace("\\", "/")
        if folder == ".":
            folder = ""
        conn.execute(
            """
            UPDATE photos SET path=?, rel_path=?, filename=?, folder=?,
            indexed_at=datetime('now') WHERE id=?
            """,
            (str(dest), str(rel).replace("\\", "/"), dest.name, folder, photo_id),
        )
    return {"id": photo_id, "rel_path": str(rel).replace("\\", "/"), "filename": dest.name}


def purge_inaccessible(
    *,
    folder_prefix: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """
    Remove index rows for files that raise OSError on access (e.g. Access Denied).
    Does not delete anything on disk — only cleans the SQLite library.
    If folder_prefix is set (e.g. 'Google Drive Old'), only that subtree is checked.
    """
    if not confirm:
        raise ValueError("confirm=True is required")

    from app.services.jobs import get_busy_status

    busy = get_busy_status()
    if busy.get("busy") and busy.get("active"):
        raise RuntimeError(
            f"Wait for {busy['active']['label']} to finish before purging index rows"
        )

    root = get_photo_root()
    prefix = (folder_prefix or "").strip().replace("\\", "/").strip("/")

    with connect() as conn:
        if prefix:
            rows = conn.execute(
                """
                SELECT id, path, rel_path, folder FROM photos
                WHERE folder = ? OR folder LIKE ? OR rel_path LIKE ?
                """,
                (prefix, f"{prefix}/%", f"{prefix}/%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, path, rel_path, folder FROM photos"
            ).fetchall()

    inaccessible: list[dict[str, Any]] = []
    readable = 0
    missing = 0
    for row in rows:
        try:
            src = assert_under_root(Path(row["path"]), root)
        except ValueError:
            inaccessible.append(
                {"id": row["id"], "rel_path": row["rel_path"], "reason": "outside root"}
            )
            continue
        try:
            if not src.exists():
                missing += 1
                inaccessible.append(
                    {"id": row["id"], "rel_path": row["rel_path"], "reason": "missing"}
                )
                continue
            # Probe read access (exists can succeed while open fails on some NAS ACLs)
            with open(src, "rb") as f:
                f.read(1)
            readable += 1
        except OSError as e:
            inaccessible.append(
                {
                    "id": row["id"],
                    "rel_path": row["rel_path"],
                    "reason": f"access denied: {e}",
                }
            )

    ids = [item["id"] for item in inaccessible]
    if ids:
        with connect() as conn:
            # delete in chunks
            for i in range(0, len(ids), 400):
                chunk = ids[i : i + 400]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM photos WHERE id IN ({placeholders})", chunk)

    return {
        "checked": len(rows),
        "readable": readable,
        "removed": len(ids),
        "missing_among_removed": missing,
        "folder_prefix": prefix or None,
        "sample": inaccessible[:40],
    }


def preview_inaccessible(
    *,
    folder_prefix: str | None = None,
) -> dict[str, Any]:
    """Count files that cannot be opened (no DB changes)."""
    root = get_photo_root()
    prefix = (folder_prefix or "").strip().replace("\\", "/").strip("/")

    with connect() as conn:
        if prefix:
            rows = conn.execute(
                """
                SELECT id, path, rel_path FROM photos
                WHERE folder = ? OR folder LIKE ? OR rel_path LIKE ?
                """,
                (prefix, f"{prefix}/%", f"{prefix}/%"),
            ).fetchall()
        else:
            # Only scan likely problem areas for speed unless prefix given
            rows = conn.execute(
                """
                SELECT id, path, rel_path FROM photos
                WHERE folder LIKE 'Google Drive Old%'
                   OR rel_path LIKE 'Google Drive Old%'
                """
            ).fetchall()

    denied = 0
    missing = 0
    ok = 0
    sample: list[dict[str, Any]] = []
    for row in rows:
        try:
            src = assert_under_root(Path(row["path"]), root)
        except ValueError:
            denied += 1
            continue
        try:
            if not src.exists():
                missing += 1
                if len(sample) < 20:
                    sample.append({"rel_path": row["rel_path"], "reason": "missing"})
                continue
            with open(src, "rb") as f:
                f.read(1)
            ok += 1
        except OSError as e:
            denied += 1
            if len(sample) < 20:
                sample.append({"rel_path": row["rel_path"], "reason": str(e)})

    return {
        "checked": len(rows),
        "ok": ok,
        "denied": denied,
        "missing": missing,
        "folder_prefix": prefix or "Google Drive Old",
        "sample": sample,
    }
