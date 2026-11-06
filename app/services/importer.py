from __future__ import annotations

import shutil
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import PHASH_THRESHOLD, THUMBS_DIR, get_photo_root
from app.database import connect
from app.paths import assert_under_root
from app.services.hashing import (
    analyze_image,
    file_sha256,
    hamming_distance,
    is_image,
    open_image,
    compute_phash,
)

MAX_IMPORT_FILES = 5000


@dataclass
class ImportState:
    job_id: int | None = None
    status: str = "idle"
    total: int = 0
    copied: int = 0
    skipped_exact: int = 0
    skipped_near: int = 0
    errors: int = 0
    message: str = ""
    cancel_requested: bool = False
    pause_requested: bool = False
    report: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = ImportState()


def get_import_status() -> dict[str, Any]:
    with _state.lock:
        processed = (
            _state.copied
            + _state.skipped_exact
            + _state.skipped_near
            + _state.errors
        )
        paused = bool(_state.pause_requested) and _state.status == "running"
        return {
            "job_id": _state.job_id,
            "status": _state.status,
            "total": _state.total,
            "processed": processed,
            "copied": _state.copied,
            "skipped_exact": _state.skipped_exact,
            "skipped_near": _state.skipped_near,
            "errors": _state.errors,
            "message": _state.message,
            "cancel_requested": _state.cancel_requested,
            "paused": paused,
            "report": list(_state.report[-200:]),
        }


def preview_import(
    source_dir: str,
    check_near: bool = True,
    threshold: int | None = None,
) -> dict[str, Any]:
    from app.services.safety import assert_import_source_allowed

    src = assert_import_source_allowed(source_dir)

    thr = PHASH_THRESHOLD if threshold is None else threshold
    files = [p for p in src.rglob("*") if p.is_file() and is_image(p)]
    if len(files) > MAX_IMPORT_FILES:
        raise ValueError(
            f"Too many files to import ({len(files):,}). "
            f"Maximum is {MAX_IMPORT_FILES:,} per preview/start."
        )

    with connect() as conn:
        hash_rows = conn.execute(
            "SELECT content_hash, id, rel_path FROM photos WHERE content_hash IS NOT NULL"
        ).fetchall()
        phash_rows = conn.execute(
            "SELECT phash, id, rel_path, content_hash FROM photos WHERE phash IS NOT NULL"
        ).fetchall()

    known_hashes = {r["content_hash"]: r for r in hash_rows}
    library_phashes = [(r["phash"], r) for r in phash_rows]

    would_copy = []
    skip_exact = []
    skip_near = []
    errors = []

    for path in files:
        try:
            ch = file_sha256(path)
            if ch in known_hashes:
                skip_exact.append(
                    {
                        "source": str(path),
                        "reason": "exact",
                        "match": known_hashes[ch]["rel_path"],
                    }
                )
                continue

            near_match = None
            if check_near:
                with open_image(path) as img:
                    ph = compute_phash(img)
                for lib_ph, row in library_phashes:
                    if hamming_distance(ph, lib_ph) <= thr:
                        # Don't treat as near if somehow same hash slipped
                        if row["content_hash"] == ch:
                            continue
                        near_match = row["rel_path"]
                        break

            if near_match:
                skip_near.append(
                    {"source": str(path), "reason": "near", "match": near_match}
                )
            else:
                would_copy.append({"source": str(path), "size": path.stat().st_size})
        except Exception as e:
            errors.append({"source": str(path), "error": str(e)})

    return {
        "source_dir": str(src),
        "total": len(files),
        "would_copy": len(would_copy),
        "skip_exact": len(skip_exact),
        "skip_near": len(skip_near),
        "errors": len(errors),
        "files_to_copy": would_copy[:500],
        "skipped_exact": skip_exact[:200],
        "skipped_near": skip_near[:200],
        "error_details": errors[:100],
    }


def start_import(
    source_dir: str,
    dest_folder: str = "Inbox",
    check_near: bool = True,
    threshold: int | None = None,
) -> dict[str, Any]:
    from app.services.jobs import JOB_IMPORT, assert_can_start
    from app.services.safety import assert_import_source_allowed

    assert_import_source_allowed(source_dir)
    assert_can_start(JOB_IMPORT)

    with _state.lock:
        if _state.status == "running":
            raise RuntimeError("An import is already running")
        _state.status = "running"
        _state.cancel_requested = False
        _state.pause_requested = False
        _state.total = 0
        _state.copied = 0
        _state.skipped_exact = 0
        _state.skipped_near = 0
        _state.errors = 0
        _state.message = "Starting import..."
        _state.report = []

    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO import_jobs(source_dir, dest_folder, status, message) VALUES(?,?,?,?)",
            (source_dir, dest_folder, "running", "Starting import..."),
        )
        job_id = int(cur.lastrowid)

    with _state.lock:
        _state.job_id = job_id

    thread = threading.Thread(
        target=_run_import,
        args=(job_id, source_dir, dest_folder, check_near, threshold),
        daemon=True,
    )
    thread.start()
    return get_import_status()


def request_cancel() -> None:
    with _state.lock:
        _state.cancel_requested = True
        _state.pause_requested = False


def request_import_pause() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No import job is running")
        if _state.cancel_requested:
            raise RuntimeError("Import is cancelling")
        _state.pause_requested = True
        _state.message = "Paused — resume when ready (current file will finish)"
    return get_import_status()


def request_import_resume() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No import job is running")
        _state.pause_requested = False
        _state.message = "Resuming import…"
    return get_import_status()


def _run_import(
    job_id: int,
    source_dir: str,
    dest_folder: str,
    check_near: bool,
    threshold: int | None,
) -> None:
    thr = PHASH_THRESHOLD if threshold is None else threshold
    try:
        root = get_photo_root()
        from app.services.safety import assert_import_source_allowed

        src = assert_import_source_allowed(source_dir)

        dest_folder_clean = dest_folder.strip().replace("\\", "/").strip("/") or "Inbox"
        dest_root = assert_under_root(root / dest_folder_clean, root)
        dest_root.mkdir(parents=True, exist_ok=True)

        files = [p for p in src.rglob("*") if p.is_file() and is_image(p)]
        if len(files) > MAX_IMPORT_FILES:
            raise ValueError(
                f"Too many files to import ({len(files):,}). "
                f"Maximum is {MAX_IMPORT_FILES:,} per preview/start."
            )
        with _state.lock:
            _state.total = len(files)
            _state.message = f"Importing {len(files):,} files…"

        with connect() as conn:
            hash_rows = conn.execute(
                "SELECT content_hash, id, rel_path FROM photos WHERE content_hash IS NOT NULL"
            ).fetchall()
            phash_rows = conn.execute(
                "SELECT phash, id, rel_path, content_hash FROM photos WHERE phash IS NOT NULL"
            ).fetchall()
        known_hashes = {r["content_hash"]: r for r in hash_rows}
        library_phashes = [(r["phash"], r) for r in phash_rows]

        from app.services.job_control import wait_while_paused

        for path in files:
            with _state.lock:
                if _state.cancel_requested:
                    _finish(job_id, "cancelled", "Import cancelled")
                    return

            if wait_while_paused(
                lock=_state.lock,
                is_cancelled=lambda: _state.cancel_requested,
                is_paused=lambda: _state.pause_requested,
            ):
                _finish(job_id, "cancelled", "Import cancelled")
                return

            try:
                ch = file_sha256(path)
                if ch in known_hashes:
                    with _state.lock:
                        _state.skipped_exact += 1
                        _state.report.append(
                            {
                                "action": "skip_exact",
                                "source": str(path),
                                "match": known_hashes[ch]["rel_path"],
                            }
                        )
                        _bump_import_message()
                    continue

                near_match = None
                ph = None
                if check_near:
                    with open_image(path) as img:
                        ph = compute_phash(img)
                    for lib_ph, row in library_phashes:
                        if hamming_distance(ph, lib_ph) <= thr:
                            near_match = row["rel_path"]
                            break

                if near_match:
                    with _state.lock:
                        _state.skipped_near += 1
                        _state.report.append(
                            {
                                "action": "skip_near",
                                "source": str(path),
                                "match": near_match,
                            }
                        )
                        _bump_import_message()
                    continue

                dest = dest_root / path.name
                if dest.exists():
                    stem, suffix = dest.stem, dest.suffix
                    n = 1
                    while dest.exists():
                        dest = dest_root / f"{stem}_{n}{suffix}"
                        n += 1

                shutil.copy2(str(path), str(dest))

                # Index the new file immediately
                thumb_dest = THUMBS_DIR / ch[:2] / f"{ch}.jpg"
                meta = analyze_image(dest, thumb_dest)
                rel = dest.relative_to(root)
                folder = str(rel.parent).replace("\\", "/")
                if folder == ".":
                    folder = ""
                stat = dest.stat()

                with connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO photos(
                            path, rel_path, filename, folder, size, mtime,
                            width, height, exif_date, latitude, longitude,
                            media_type, duration, content_hash, phash, thumb_path
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(dest),
                            str(rel).replace("\\", "/"),
                            dest.name,
                            folder,
                            stat.st_size,
                            stat.st_mtime,
                            meta["width"],
                            meta["height"],
                            meta["exif_date"],
                            meta.get("latitude"),
                            meta.get("longitude"),
                            meta.get("media_type") or "image",
                            meta.get("duration"),
                            meta["content_hash"],
                            meta["phash"],
                            meta["thumb_path"],
                        ),
                    )

                known_hashes[meta["content_hash"]] = {"rel_path": str(rel)}
                library_phashes.append((meta["phash"], {"rel_path": str(rel), "content_hash": meta["content_hash"]}))

                with _state.lock:
                    _state.copied += 1
                    _state.report.append(
                        {"action": "copied", "source": str(path), "dest": str(rel).replace("\\", "/")}
                    )
                    _bump_import_message()
            except Exception as e:
                with _state.lock:
                    _state.errors += 1
                    _state.report.append(
                        {"action": "error", "source": str(path), "error": str(e)}
                    )
                    _bump_import_message()

        with _state.lock:
            msg = (
                f"Import done. copied={_state.copied} "
                f"exact_skip={_state.skipped_exact} near_skip={_state.skipped_near} "
                f"errors={_state.errors}"
            )
        _finish(job_id, "completed", msg)
    except Exception as e:
        _finish(job_id, "error", f"{e}\n{traceback.format_exc()}")


def _bump_import_message() -> None:
    """Caller must hold _state.lock."""
    done = (
        _state.copied
        + _state.skipped_exact
        + _state.skipped_near
        + _state.errors
    )
    _state.message = (
        f"copied={_state.copied} exact_skip={_state.skipped_exact} "
        f"near_skip={_state.skipped_near} errors={_state.errors} · "
        f"{done:,}/{_state.total:,}"
    )


def _finish(job_id: int, status: str, message: str) -> None:
    from app.services.jobs import JOB_IMPORT, release_job

    with _state.lock:
        _state.status = status
        _state.message = message
        copied = _state.copied
        skipped_exact = _state.skipped_exact
        skipped_near = _state.skipped_near
        errors = _state.errors
        total = _state.total

    with connect() as conn:
        conn.execute(
            """
            UPDATE import_jobs SET
                status=?, total=?, copied=?, skipped_exact=?, skipped_near=?,
                errors=?, message=?, finished_at=datetime('now')
            WHERE id=?
            """,
            (status, total, copied, skipped_exact, skipped_near, errors, message[:2000], job_id),
        )
    release_job(JOB_IMPORT)
