from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import app.config as config
from app.config import get_photo_root, get_scan_workers
from app.database import connect, open_connection, run_with_retry, rows_to_dicts, strip_photo_path
from app.services.hashing import (
    analyze_image,
    analyze_video,
    is_media,
    is_video,
    is_video_placeholder_thumb,
    make_thumbnail,
    make_video_thumbnail,
    open_image,
)

# Heuristics for upfront duration estimates (seconds per file over SMB).
_SEC_PER_HASH = 1.8  # read + SHA-256 + pHash + thumb
_SEC_PER_SKIP = 0.05  # already indexed, size/mtime match
_LONG_SCAN_SECONDS = 5 * 60
_LONG_SCAN_FILES = 1500
_BATCH_COMMIT = 25
_SKIP_FILENAMES = {
    "thumbs.db",
    "desktop.ini",
    ".ds_store",
    "autorun.inf",
}


def _should_skip_file(name: str) -> bool:
    """Skip OS junk that looks like media by extension but isn't (e.g. macOS AppleDouble)."""
    lower = name.lower()
    if lower in _SKIP_FILENAMES:
        return True
    # AppleDouble / resource-fork sidecars from macOS / Google Drive (often 4KB "._photo.jpg")
    if name.startswith("._"):
        return True
    return False


@dataclass
class ScanState:
    job_id: int | None = None
    status: str = "idle"  # idle | running | completed | error | cancelled
    phase: str = "idle"  # idle | scanning | finishing
    total: int = 0  # best-known library size (prior estimate or found so far)
    processed: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0  # already indexed, unchanged
    removed: int = 0
    errors: int = 0
    discovered: int = 0
    bytes_processed: int = 0  # sum of file sizes handled this run
    bytes_added: int = 0
    bytes_skipped: int = 0
    message: str = ""
    started_at: float | None = None
    index_started_at: float | None = None
    estimate_seconds: float | None = None
    eta_seconds: float | None = None
    rate_per_sec: float | None = None
    rate_bytes_per_sec: float | None = None
    long_scan: bool = False
    resumable: bool = False
    already_indexed: int = 0
    cancel_requested: bool = False
    pause_requested: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


_state = ScanState()


def recover_orphaned_jobs() -> int:
    """Mark scan/import jobs left as 'running' after a crash as 'interrupted'."""
    with connect() as conn:
        cur1 = conn.execute(
            "UPDATE scan_jobs SET status='interrupted', "
            "message=COALESCE(message, '') || ' (interrupted on startup)', "
            "finished_at=datetime('now') WHERE status='running'"
        )
        cur2 = conn.execute(
            "UPDATE import_jobs SET status='interrupted', "
            "message=COALESCE(message, '') || ' (interrupted on startup)', "
            "finished_at=datetime('now') WHERE status='running'"
        )
        return int(cur1.rowcount or 0) + int(cur2.rowcount or 0)


def _format_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    secs = int(round(seconds))
    if secs < 60:
        return f"~{secs}s"
    mins = secs // 60
    rem = secs % 60
    if mins < 60:
        return f"~{mins}m {rem}s" if rem else f"~{mins}m"
    hours = mins // 60
    mins = mins % 60
    return f"~{hours}h {mins}m"


def _last_scan_job() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, status, total, processed, message FROM scan_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _photo_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"])


def _compute_resumable() -> tuple[bool, int, int]:
    """Return (resumable, already_indexed, prior_total_estimate)."""
    already = _photo_count()
    last = _last_scan_job()
    prior_total = int(last["total"] or 0) if last else 0
    if last and last["status"] in ("error", "cancelled", "interrupted"):
        prior_total = max(prior_total, int(last["processed"] or 0))
        return True, already, prior_total
    if already > 0 and last and last["status"] != "completed":
        return True, already, max(prior_total, already)
    # Incremental re-scan is always a soft resume when library exists
    if already > 0:
        return True, already, max(prior_total, already)
    return False, 0, 0


def _volume_usage(root: Path | None = None) -> dict[str, int | None]:
    """Used/free/total bytes for the PHOTO_ROOT volume (whole share, not photos-only)."""
    try:
        path = root or get_photo_root()
        usage = shutil.disk_usage(path)
        return {
            "volume_total_bytes": int(usage.total),
            "volume_used_bytes": int(usage.used),
            "volume_free_bytes": int(usage.free),
        }
    except Exception:
        return {
            "volume_total_bytes": None,
            "volume_used_bytes": None,
            "volume_free_bytes": None,
        }


def get_scan_status() -> dict[str, Any]:
    resumable, already, prior = _compute_resumable()
    lib = library_stats()
    with _state.lock:
        elapsed = (time.time() - _state.started_at) if _state.started_at else None
        live_resumable = _state.resumable if _state.status == "running" else resumable
        live_already = _state.already_indexed if _state.status == "running" else already
        return {
            "job_id": _state.job_id,
            "status": _state.status,
            "phase": _state.phase,
            "total": _state.total,
            "processed": _state.processed,
            "added": _state.added,
            "updated": _state.updated,
            "skipped": _state.skipped,
            "removed": _state.removed,
            "errors": _state.errors,
            "discovered": _state.discovered,
            "bytes_processed": _state.bytes_processed,
            "bytes_added": _state.bytes_added,
            "bytes_skipped": _state.bytes_skipped,
            "library_bytes": lib.get("total_bytes") or 0,
            "library_count": lib.get("photo_count") or 0,
            "volume_total_bytes": lib.get("volume_total_bytes"),
            "volume_used_bytes": lib.get("volume_used_bytes"),
            "volume_free_bytes": lib.get("volume_free_bytes"),
            "message": _state.message,
            "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
            "estimate_seconds": (
                round(_state.estimate_seconds, 1) if _state.estimate_seconds is not None else None
            ),
            "eta_seconds": round(_state.eta_seconds, 1) if _state.eta_seconds is not None else None,
            "estimate_label": _format_duration(_state.estimate_seconds),
            "eta_label": _format_duration(_state.eta_seconds),
            "rate_per_sec": (
                round(_state.rate_per_sec, 2) if _state.rate_per_sec is not None else None
            ),
            "rate_bytes_per_sec": (
                round(_state.rate_bytes_per_sec, 1)
                if _state.rate_bytes_per_sec is not None
                else None
            ),
            "long_scan": _state.long_scan,
            "resumable": live_resumable,
            "already_indexed": live_already,
            "prior_total": prior,
            "paused": bool(_state.pause_requested) and _state.status == "running",
            "cancel_requested": _state.cancel_requested,
        }


def request_cancel() -> None:
    with _state.lock:
        _state.cancel_requested = True
        _state.pause_requested = False


def request_pause() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No scan is running")
        if _state.cancel_requested:
            raise RuntimeError("Scan is cancelling")
        _state.pause_requested = True
        _state.phase = "paused"
        _state.message = "Paused — resume when ready (in-flight files will finish)"
    return get_scan_status()


def request_resume() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No scan is running")
        _state.pause_requested = False
        if _state.phase == "paused":
            _state.phase = "scanning"
        _state.message = "Resuming scan…"
    return get_scan_status()


def start_scan(full: bool = False) -> dict[str, Any]:
    from app.services.jobs import JOB_SCAN, assert_can_start

    assert_can_start(JOB_SCAN)

    resumable, already, prior_total = _compute_resumable()
    with _state.lock:
        if _state.status == "running":
            raise RuntimeError("A scan is already running")
        _state.status = "running"
        _state.phase = "scanning"
        _state.cancel_requested = False
        _state.pause_requested = False
        _state.total = prior_total if not full else 0
        _state.processed = 0
        _state.added = 0
        _state.updated = 0
        _state.skipped = 0
        _state.removed = 0
        _state.errors = 0
        _state.discovered = 0
        _state.bytes_processed = 0
        _state.bytes_added = 0
        _state.bytes_skipped = 0
        _state.started_at = time.time()
        _state.index_started_at = time.time()
        _state.estimate_seconds = None
        _state.eta_seconds = None
        _state.rate_per_sec = None
        _state.rate_bytes_per_sec = None
        _state.long_scan = prior_total >= _LONG_SCAN_FILES or already >= _LONG_SCAN_FILES
        _state.resumable = resumable and not full
        _state.already_indexed = already
        if full:
            _state.message = "Full scan - rehashing every photo..."
        elif resumable and already > 0:
            _state.message = (
                f"Resuming - {already:,} already indexed; "
                "unchanged files will be skipped"
            )
            if prior_total > already:
                remaining = prior_total - already
                est = remaining * _SEC_PER_HASH
                _state.estimate_seconds = est
                _state.eta_seconds = est
                _state.long_scan = _state.long_scan or est >= _LONG_SCAN_SECONDS
                _state.message += f" | ~{remaining:,} left from last run | est. {_format_duration(est)}"
        else:
            _state.message = "Scanning library (indexes as it goes - safe to resume if interrupted)..."
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO scan_jobs(status, total, message) VALUES('running', ?, ?)",
            (prior_total, _state.message),
        )
        job_id = int(cur.lastrowid)
    with _state.lock:
        _state.job_id = job_id
    thread = threading.Thread(target=_run_scan, args=(job_id, full), daemon=True)
    thread.start()
    return get_scan_status()


def _update_live_eta() -> None:
    """Update rate / ETA from scanning progress. Caller must hold lock."""
    if not _state.index_started_at or _state.processed <= 0:
        return
    elapsed = time.time() - _state.index_started_at
    if elapsed < 1:
        return
    rate = _state.processed / elapsed
    _state.rate_per_sec = rate
    if _state.bytes_processed > 0:
        _state.rate_bytes_per_sec = _state.bytes_processed / elapsed
    # total is a best guess (prior run or found-so-far)
    if _state.total > _state.processed and rate > 0:
        _state.eta_seconds = (_state.total - _state.processed) / rate
        if _state.eta_seconds >= _LONG_SCAN_SECONDS:
            _state.long_scan = True
    elif _state.added + _state.updated > 5 and rate > 0:
        # No reliable total yet - rough remaining based on hash-heavy pace
        _state.eta_seconds = None


def _volume_usage(root: Path | None = None) -> dict[str, int | None]:
    """Used/free/total bytes for the PHOTO_ROOT volume (whole share, not photos-only)."""
    try:
        path = root or get_photo_root()
        usage = shutil.disk_usage(path)
        return {
            "volume_total_bytes": int(usage.total),
            "volume_used_bytes": int(usage.used),
            "volume_free_bytes": int(usage.free),
        }
    except Exception:
        return {
            "volume_total_bytes": None,
            "volume_used_bytes": None,
            "volume_free_bytes": None,
        }


def _thumb_path_for(photo_id_or_hash: str) -> Path:
    # Nest thumbs to avoid huge single directories
    name = photo_id_or_hash.replace("/", "_").replace("\\", "_")
    sub = name[:2] if len(name) >= 2 else "00"
    return config.THUMBS_DIR / sub / f"{name}.jpg"


_thumb_regen_locks: dict[str, threading.Lock] = {}
_thumb_regen_guard = threading.Lock()


def _lock_for_thumb(key: str) -> threading.Lock:
    with _thumb_regen_guard:
        lock = _thumb_regen_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thumb_regen_locks[key] = lock
        return lock


def ensure_library_thumb(photo_id: int) -> Path:
    """
    Return a local JPEG thumb for a library photo, regenerating from the
    original when the cached file is missing (common after thumb cache loss).
    """
    photo = get_photo_raw(photo_id)
    if not photo:
        raise FileNotFoundError(f"Photo not found: {photo_id}")

    content_hash = (photo.get("content_hash") or "").strip()
    if content_hash:
        dest = _thumb_path_for(content_hash)
    elif photo.get("thumb_path"):
        dest = Path(photo["thumb_path"])
    else:
        dest = _thumb_path_for(f"id-{photo_id}")

    media_type = (photo.get("media_type") or "image").lower()
    orig = Path(photo["path"])
    is_vid = media_type == "video" or is_video(orig)

    def _usable(p: Path) -> bool:
        if not p.exists() or p.stat().st_size <= 0:
            return False
        # Upgrade old solid VIDEO placeholders to a real frame when possible.
        if is_vid and is_video_placeholder_thumb(p):
            return False
        return True

    if _usable(dest):
        return dest

    # Stale DB path pointing elsewhere — use it if present
    stored = photo.get("thumb_path")
    if stored:
        stored_path = Path(stored)
        if stored_path != dest and _usable(stored_path):
            return stored_path

    if not orig.exists():
        raise FileNotFoundError(f"Original missing on disk: {orig}")

    lock = _lock_for_thumb(str(dest))
    with lock:
        if _usable(dest):
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".regen.tmp.jpg")
        try:
            if is_vid:
                make_video_thumbnail(tmp, video_path=orig)
            else:
                img = open_image(orig)
                try:
                    make_thumbnail(img, tmp)
                finally:
                    img.close()
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        if stored != str(dest):
            with connect() as conn:
                conn.execute(
                    "UPDATE photos SET thumb_path = ? WHERE id = ?",
                    (str(dest), photo_id),
                )

    return dest


def _should_skip_dir(name: str) -> bool:
    lower = name.lower()
    return (
        name.startswith("_quarantine")
        or lower in {
            ".trash",
            "#recycle",
            "@eadir",
            "system volume information",
            # Locked / unreadable on this NAS share (Access Denied on many files)
            "google drive old",
        }
    )


def _iter_media_files(root: Path, on_entry=None):
    """
    Yield (path, size, mtime) for images and videos under root using os.scandir.
    Passes DirEntry stat so callers can skip without Path.stat().
    """
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if on_entry is not None:
                        on_entry(entry)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not _should_skip_dir(entry.name):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if _should_skip_file(entry.name):
                        continue
                    path = Path(entry.path)
                    if not is_media(path):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield path, st.st_size, st.st_mtime
        except OSError:
            continue


# Back-compat alias
_iter_image_files = _iter_media_files


def _finalize_thumb(meta: dict[str, Any], temp_thumb: Path) -> None:
    final_thumb = _thumb_path_for(meta["content_hash"])
    if final_thumb != temp_thumb:
        final_thumb.parent.mkdir(parents=True, exist_ok=True)
        if temp_thumb.exists():
            if final_thumb.exists():
                temp_thumb.unlink(missing_ok=True)
            else:
                temp_thumb.replace(final_thumb)
        meta["thumb_path"] = str(final_thumb)
    else:
        meta["thumb_path"] = str(temp_thumb)


def _write_photo_row(
    conn,
    *,
    abs_path: str,
    path: Path,
    root: Path,
    size: int,
    mtime: float,
    meta: dict[str, Any],
    prev: dict | None,
    existing: dict[str, dict],
) -> str:
    """Insert or update one photo row. Caller holds write lock / owns conn."""
    rel = path.relative_to(root)
    folder = str(rel.parent).replace("\\", "/")
    if folder == ".":
        folder = ""
    rel_str = str(rel).replace("\\", "/")
    if prev is None:
        cur = conn.execute(
            """
            INSERT INTO photos(
                path, rel_path, filename, folder, size, mtime,
                width, height, exif_date, latitude, longitude,
                media_type, duration, content_hash, phash, thumb_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                abs_path,
                rel_str,
                path.name,
                folder,
                size,
                mtime,
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
        photo_id = int(cur.lastrowid)
        action = "added"
    else:
        conn.execute(
            """
            UPDATE photos SET
                path=?, rel_path=?, filename=?, folder=?, size=?, mtime=?,
                width=?, height=?,
                exif_date=COALESCE(?, exif_date),
                latitude=COALESCE(?, latitude),
                longitude=COALESCE(?, longitude),
                media_type=?, duration=?, content_hash=?,
                phash=?, thumb_path=?, indexed_at=datetime('now')
            WHERE id=?
            """,
            (
                abs_path,
                rel_str,
                path.name,
                folder,
                size,
                mtime,
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
                prev["id"],
            ),
        )
        photo_id = prev["id"]
        action = "updated"
    existing[abs_path] = {
        "id": photo_id,
        "path": abs_path,
        "size": size,
        "mtime": mtime,
        "content_hash": meta["content_hash"],
    }
    return action


def _bump_progress(action: str, size: int = 0) -> bool:
    """Update live counters; return whether progress should be persisted."""
    with _state.lock:
        _state.processed += 1
        _state.discovered = _state.processed
        size = max(0, int(size or 0))
        _state.bytes_processed += size
        if action == "added":
            _state.added += 1
            _state.bytes_added += size
        elif action == "updated":
            _state.updated += 1
        else:
            _state.skipped += 1
            _state.bytes_skipped += size
        if _state.processed > _state.total:
            _state.total = _state.processed
        _update_live_eta()
        if _state.processed % 10 == 0 or action != "skipped":
            eta = _format_duration(_state.eta_seconds)
            eta_bit = f" | ETA {eta}" if eta else ""
            mb = _state.bytes_processed / (1024 * 1024)
            _state.message = (
                f"Scanning... {_state.processed:,} files / {mb:,.0f} MB "
                f"(+{_state.added:,} ~{_state.updated:,} "
                f"skip {_state.skipped:,}){eta_bit}"
            )
        return _state.processed % _BATCH_COMMIT == 0


def _analyze_worker(path: Path, thumb_dest: Path) -> dict[str, Any]:
    # Videos: skip Pillow analyze; images get full analyze (incl. GPS)
    if is_video(path):
        return analyze_video(path, thumb_dest)
    return analyze_image(path, thumb_dest)


def _run_scan(job_id: int, full: bool) -> None:
    try:
        root = get_photo_root()
    except ValueError as e:
        _finish_error(job_id, str(e))
        return
    try:
        with connect() as conn:
            existing_rows = conn.execute(
                "SELECT id, path, size, mtime, content_hash FROM photos"
            ).fetchall()
        existing = {os.path.normpath(r["path"]): dict(r) for r in existing_rows}
        original_ids = {os.path.normpath(r["path"]): r["id"] for r in existing_rows}
        seen_paths: set[str] = set()
        walked = 0
        write_lock = threading.Lock()
        pending_since_commit = 0
        walk_completed = False

        with _state.lock:
            _state.message = f"Scanning {root} (incremental - safe to resume)..."
            _state.phase = "scanning"

        def _on_entry(_entry) -> None:
            nonlocal walked
            walked += 1
            if walked == 1 or walked % 50 == 0:
                with _state.lock:
                    _state.message = (
                        f"Scanning... seen {walked:,} items, "
                        f"{_state.processed:,} photos "
                        f"(+{_state.added:,} skip {_state.skipped:,})"
                    )

        workers = get_scan_workers()
        futures: dict[Future, tuple[Path, str, int, float, dict | None, Path]] = {}
        conn = open_connection()

        def _flush_commit() -> None:
            nonlocal pending_since_commit

            def _do() -> None:
                conn.commit()
                _persist_progress(job_id)

            run_with_retry(_do, label="scan flush")
            pending_since_commit = 0

        def _record_error(name: str, err: Exception) -> None:
            with _state.lock:
                _state.errors += 1
                _state.message = f"Error: {name}: {err}"
                _state.processed += 1
                _state.discovered = _state.processed
                if _state.processed > _state.total:
                    _state.total = _state.processed

        def _record_access_skip(name: str, err: Exception) -> None:
            """NAS permission / network denials — not corrupt files."""
            with _state.lock:
                _state.skipped += 1
                _state.processed += 1
                _state.discovered = _state.processed
                if _state.processed > _state.total:
                    _state.total = _state.processed
                if _state.skipped <= 5 or _state.skipped % 50 == 0:
                    _state.message = f"Skipped inaccessible: {name}: {err}"

        def _handle_result(
            path: Path,
            abs_path: str,
            size: int,
            mtime: float,
            prev: dict | None,
            temp_thumb: Path,
            meta: dict[str, Any],
        ) -> None:
            nonlocal pending_since_commit
            _finalize_thumb(meta, temp_thumb)
            with write_lock:
                action = _write_photo_row(
                    conn,
                    abs_path=abs_path,
                    path=path,
                    root=root,
                    size=size,
                    mtime=mtime,
                    meta=meta,
                    prev=prev,
                    existing=existing,
                )
                pending_since_commit += 1
                should_persist = _bump_progress(action, size)
                if pending_since_commit >= _BATCH_COMMIT or should_persist:
                    _flush_commit()

        def _consume_future(fut: Future) -> None:
            path, abs_path, size, mtime, prev, temp_thumb = futures.pop(fut)
            try:
                meta = fut.result()
                _handle_result(path, abs_path, size, mtime, prev, temp_thumb, meta)
            except (PermissionError, OSError) as e:
                # WinError 5 / 59 on SMB — treat as skip, not a corrupt-file error
                winerr = getattr(e, "winerror", None)
                if isinstance(e, PermissionError) or winerr in (5, 32, 33, 59):
                    _record_access_skip(path.name, e)
                else:
                    _record_error(path.name, e)
            except Exception as e:
                _record_error(path.name, e)

        def _drain_done() -> None:
            for fut in [f for f in list(futures) if f.done()]:
                _consume_future(fut)

        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                def _announce_pause() -> None:
                    with _state.lock:
                        _state.phase = "paused"
                        _state.message = "Paused — click Resume when ready"

                for path, size, mtime in _iter_media_files(root, on_entry=_on_entry):
                    from app.services.job_control import wait_while_paused

                    if wait_while_paused(
                        lock=_state.lock,
                        is_cancelled=lambda: _state.cancel_requested,
                        is_paused=lambda: _state.pause_requested,
                        set_paused_ui=_announce_pause,
                    ):
                        for fut in list(futures):
                            fut.cancel()
                        while futures:
                            _consume_future(next(as_completed(futures)))
                        if pending_since_commit:
                            _flush_commit()
                        _finish_cancelled(job_id)
                        return

                    if _cancelled():
                        for fut in list(futures):
                            fut.cancel()
                        # Finish in-flight analyzes so thumbs/DB stay consistent
                        while futures:
                            _consume_future(next(as_completed(futures)))
                        if pending_since_commit:
                            _flush_commit()
                        _finish_cancelled(job_id)
                        return

                    abs_path = os.path.normpath(str(path))
                    seen_paths.add(abs_path)
                    prev = existing.get(abs_path)
                    needs_update = (
                        full
                        or prev is None
                        or prev["size"] != size
                        or abs(prev["mtime"] - mtime) > 0.5
                    )
                    if not needs_update:
                        with write_lock:
                            should_persist = _bump_progress("skipped", size)
                            if should_persist:
                                _flush_commit()
                        continue

                    thumb_key = abs_path.encode("utf-8", errors="ignore").hex()[:40]
                    temp_thumb = _thumb_path_for(thumb_key)
                    fut = executor.submit(_analyze_worker, path, temp_thumb)
                    futures[fut] = (path, abs_path, size, mtime, prev, temp_thumb)

                    # Bound in-flight work
                    while len(futures) >= workers * 2:
                        _drain_done()
                        if len(futures) >= workers * 2:
                            _consume_future(next(as_completed(futures)))

                while futures:
                    _consume_future(next(as_completed(futures)))

            if pending_since_commit:
                _flush_commit()
            walk_completed = True

            # Safer prune: only if walk finished cleanly and error rate is acceptable
            with _state.lock:
                processed = _state.processed
                errors = _state.errors
                cancelled = _state.cancel_requested
            can_prune = (
                walk_completed
                and not cancelled
                and (processed <= 100 or (errors / max(processed, 1)) < 0.05)
            )

            removed = 0
            prune_note = ""
            if can_prune:
                with _state.lock:
                    _state.phase = "finishing"
                    _state.message = "Finishing - removing missing files from index..."
                stale_ids = [
                    photo_id
                    for path_str, photo_id in original_ids.items()
                    if path_str not in seen_paths
                ]
                if stale_ids:
                    with write_lock:
                        conn.executemany(
                            "DELETE FROM photos WHERE id = ?",
                            [(i,) for i in stale_ids],
                        )
                        conn.commit()
                    removed = len(stale_ids)
            else:
                prune_note = " | Skipped prune (incomplete walk)"
                with _state.lock:
                    _state.phase = "finishing"
                    _state.message = "Finishing - skipped prune (incomplete walk)..."

            with _state.lock:
                _state.removed = removed
                _state.status = "completed"
                _state.phase = "idle"
                _state.total = _state.processed
                _state.eta_seconds = 0
                _state.resumable = False
                elapsed = (time.time() - _state.started_at) if _state.started_at else 0
                _state.message = (
                    f"Done in {_format_duration(elapsed) or '?'} | "
                    f"+{_state.added} ~{_state.updated} skip {_state.skipped} "
                    f"-{removed} errors={_state.errors}{prune_note}"
                )
                message = _state.message
                added, updated, errors, processed, total = (
                    _state.added,
                    _state.updated,
                    _state.errors,
                    _state.processed,
                    _state.total,
                )
            with write_lock:
                conn.execute(
                    """
                    UPDATE scan_jobs SET
                        status='completed', total=?, processed=?, added=?, updated=?, removed=?,
                        errors=?, message=?, finished_at=datetime('now')
                    WHERE id=?
                    """,
                    (total, processed, added, updated, removed, errors, message, job_id),
                )
                conn.commit()
            from app.services.jobs import JOB_SCAN, release_job

            release_job(JOB_SCAN)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        _finish_error(job_id, f"{e}\n{traceback.format_exc()}")


def _cancelled() -> bool:
    with _state.lock:
        return _state.cancel_requested


def _persist_progress(job_id: int) -> None:
    with _state.lock:
        payload = (
            max(_state.total, _state.processed),
            _state.processed,
            _state.added,
            _state.updated,
            _state.removed,
            _state.errors,
            _state.message,
            job_id,
        )

    def _write() -> None:
        with connect() as conn:
            conn.execute(
                """
                UPDATE scan_jobs SET
                    total=?, processed=?, added=?, updated=?, removed=?, errors=?, message=?
                WHERE id=?
                """,
                payload,
            )

    run_with_retry(_write, label="scan progress")


def _finish_error(job_id: int, message: str) -> None:
    from app.services.jobs import JOB_SCAN, release_job

    with _state.lock:
        _state.status = "error"
        _state.phase = "idle"
        _state.resumable = True
        # Persist how far we got so the next run can estimate remaining work
        total = max(_state.total, _state.processed, _state.discovered)
        _state.total = total
        processed = _state.processed
        _state.message = message
    with connect() as conn:
        conn.execute(
            """
            UPDATE scan_jobs SET status='error', total=?, processed=?, message=?,
            finished_at=datetime('now') WHERE id=?
            """,
            (total, processed, message[:2000], job_id),
        )
    release_job(JOB_SCAN)


def _finish_cancelled(job_id: int) -> None:
    from app.services.jobs import JOB_SCAN, release_job

    with _state.lock:
        _state.status = "cancelled"
        _state.phase = "idle"
        _state.resumable = True
        total = max(_state.total, _state.processed, _state.discovered)
        _state.total = total
        processed = _state.processed
        _state.message = "Scan cancelled - click Resume to continue"
    with connect() as conn:
        conn.execute(
            """
            UPDATE scan_jobs SET status='cancelled', total=?, processed=?,
            message=?, finished_at=datetime('now') WHERE id=?
            """,
            (total, processed, _state.message, job_id),
        )
    release_job(JOB_SCAN)


def library_stats() -> dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
        folders = conn.execute("SELECT COUNT(DISTINCT folder) AS c FROM photos").fetchone()["c"]
        size = conn.execute("SELECT COALESCE(SUM(size), 0) AS s FROM photos").fetchone()["s"]
        dated = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE exif_date IS NOT NULL"
        ).fetchone()["c"]
    out = {
        "photo_count": total,
        "folder_count": folders,
        "total_bytes": size,
        "with_exif_date": dated,
    }
    out.update(_volume_usage())
    return out


def list_folders() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT folder, COUNT(*) AS count
            FROM photos
            GROUP BY folder
            ORDER BY folder
            """
        ).fetchall()
    return rows_to_dicts(rows)


# Folder segments that are storage layout, not browse-by-name labels.
_PATH_NAME_SKIP = frozenset(
    {
        "",
        "_quarantine_duplicates",
        "camera roll",
        "cameraroll",
        "dcim",
        "downloads",
        "fromicloud",
        "icloud",
        "inbox",
        "live photos",
        "originals",
        "photo library",
        "photos",
        "pictures",
        "quarantine",
        "screenshots",
        "thumbs",
        "videos",
    }
)


def _is_browse_name(segment: str) -> bool:
    name = (segment or "").strip()
    if len(name) < 2 or len(name) > 64:
        return False
    if name.lower() in _PATH_NAME_SKIP:
        return False
    if name.isdigit():
        return False
    # Years / year-months from camera dumps
    if len(name) == 4 and name.isdigit():
        return False
    if len(name) == 7 and name[4] == "-" and name[:4].isdigit() and name[5:].isdigit():
        return False
    # Mostly punctuation / UUID-like noise
    alnum = sum(1 for ch in name if ch.isalnum())
    if alnum < 2:
        return False
    return True


def list_path_names(*, q: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """
    Distinct folder-path segments with photo counts for Browse-by-name.

    A photo in Family/Jessica/2020 counts once toward Jessica (path text match),
    not face identity — that stays under People.
    """
    limit = max(1, min(int(limit), 1000))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT folder, COUNT(*) AS count
            FROM photos
            GROUP BY folder
            """
        ).fetchall()

    # lower -> (display_name, count)
    agg: dict[str, list[Any]] = {}
    for row in rows:
        folder = str(row["folder"] or "").replace("\\", "/")
        n = int(row["count"] or 0)
        if n <= 0:
            continue
        seen_in_folder: set[str] = set()
        for part in folder.split("/"):
            if not _is_browse_name(part):
                continue
            key = part.casefold()
            if key in seen_in_folder:
                continue
            seen_in_folder.add(key)
            cur = agg.get(key)
            if cur is None:
                agg[key] = [part, n]
            else:
                cur[1] += n
                # Prefer Title Case / longer display when counts tie-ish
                if part[:1].isupper() and not str(cur[0])[:1].isupper():
                    cur[0] = part

    items = [{"name": display, "count": count} for display, count in agg.values()]
    needle = (q or "").strip().casefold()
    if needle:
        items = [i for i in items if needle in str(i["name"]).casefold()]
    items.sort(key=lambda i: (-int(i["count"]), str(i["name"]).casefold()))
    return items[:limit]


# Whitelisted ORDER BY clauses for Browse / People photo grids.
PHOTO_SORTS: dict[str, str] = {
    "date_desc": "COALESCE(exif_date, '') DESC, filename COLLATE NOCASE ASC, id DESC",
    "date_asc": "COALESCE(exif_date, '') ASC, filename COLLATE NOCASE ASC, id ASC",
    "mtime_desc": "mtime DESC, filename COLLATE NOCASE ASC, id DESC",
    "mtime_asc": "mtime ASC, filename COLLATE NOCASE ASC, id ASC",
    "name_asc": "filename COLLATE NOCASE ASC, COALESCE(exif_date, '') DESC, id ASC",
    "name_desc": "filename COLLATE NOCASE DESC, COALESCE(exif_date, '') DESC, id DESC",
}
PHOTO_SORTS_ALIASED: dict[str, str] = {
    "date_desc": "COALESCE({a}.exif_date, '') DESC, {a}.filename COLLATE NOCASE ASC, {a}.id DESC",
    "date_asc": "COALESCE({a}.exif_date, '') ASC, {a}.filename COLLATE NOCASE ASC, {a}.id ASC",
    "mtime_desc": "{a}.mtime DESC, {a}.filename COLLATE NOCASE ASC, {a}.id DESC",
    "mtime_asc": "{a}.mtime ASC, {a}.filename COLLATE NOCASE ASC, {a}.id ASC",
    "name_asc": "{a}.filename COLLATE NOCASE ASC, COALESCE({a}.exif_date, '') DESC, {a}.id ASC",
    "name_desc": "{a}.filename COLLATE NOCASE DESC, COALESCE({a}.exif_date, '') DESC, {a}.id DESC",
}
DEFAULT_PHOTO_SORT = "date_desc"


def resolve_photo_sort(sort: str | None = None) -> str:
    key = (sort or DEFAULT_PHOTO_SORT).strip().lower()
    return key if key in PHOTO_SORTS else DEFAULT_PHOTO_SORT


def photo_order_sql(sort: str | None = None, *, table_alias: str | None = None) -> str:
    key = resolve_photo_sort(sort)
    if table_alias:
        return PHOTO_SORTS_ALIASED[key].format(a=table_alias)
    return PHOTO_SORTS[key]


def list_photos(
    folder: str | None = None,
    year: str | None = None,
    month: str | None = None,
    q: str | None = None,
    person_id: int | None = None,
    sort: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if folder is not None:
        clauses.append("folder = ?")
        params.append(folder)
    # Year/month filters = when the file landed on the drive (mtime), not camera EXIF.
    if year:
        clauses.append(
            "strftime('%Y', datetime(mtime, 'unixepoch', 'localtime')) = ?"
        )
        params.append(year)
    if month and year:
        clauses.append(
            "strftime('%Y-%m', datetime(mtime, 'unixepoch', 'localtime')) = ?"
        )
        params.append(f"{year}-{month.zfill(2)}")
    if q:
        clauses.append("(filename LIKE ? OR folder LIKE ? OR rel_path LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if person_id is not None:
        clauses.append(
            "id IN (SELECT DISTINCT photo_id FROM faces "
            "WHERE person_id = ? AND ignored = 0)"
        )
        params.append(int(person_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sort_key = resolve_photo_sort(sort)
    order_by = photo_order_sql(sort_key)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM photos {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT id, rel_path, filename, folder, size, width, height,
                   exif_date, latitude, longitude, media_type, duration, content_hash
            FROM photos
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort_key,
        "person_id": person_id,
        "items": rows_to_dicts(rows),
    }


def list_map_points(limit: int = 5000) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, latitude AS lat, longitude AS lon, rel_path, exif_date
            FROM photos
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            ORDER BY COALESCE(exif_date, '') DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows_to_dicts(rows)


def list_timeline() -> list[dict[str, Any]]:
    """Months when files were added/modified on the drive (mtime), newest first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT strftime('%Y-%m', datetime(mtime, 'unixepoch', 'localtime')) AS month,
                   COUNT(*) AS count
            FROM photos
            WHERE mtime IS NOT NULL AND mtime > 0
            GROUP BY month
            ORDER BY month DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


def get_photo(photo_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    return strip_photo_path(dict(row)) if row else None


def get_photo_raw(photo_id: int) -> dict[str, Any] | None:
    """Internal: include absolute path for file serving / organize."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None
