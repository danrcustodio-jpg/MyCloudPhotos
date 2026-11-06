"""Convert camera RAW files to JPEG and quarantine the originals."""

from __future__ import annotations

import os
import shutil
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from app.config import (
    QUARANTINE_RAW_DIR_NAME,
    RAW_EXTENSIONS,
    THUMBS_DIR,
    get_photo_root,
)
from app.database import connect
from app.paths import assert_under_root
from app.services.hashing import analyze_image, decode_raw_to_image
from app.services.scanner import _should_skip_dir

_JPEG_QUALITY = 92


@dataclass
class RawConvertState:
    status: str = "idle"
    phase: str = ""
    total: int = 0
    processed: int = 0
    converted: int = 0
    quarantined: int = 0
    skipped: int = 0
    errors: int = 0
    message: str = ""
    report: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    pause_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = RawConvertState()


def get_raw_convert_status() -> dict[str, Any]:
    with _state.lock:
        paused = bool(_state.pause_requested) and _state.status == "running"
        return {
            "status": _state.status,
            "phase": "paused" if paused else _state.phase,
            "total": _state.total,
            "processed": _state.processed,
            "converted": _state.converted,
            "quarantined": _state.quarantined,
            "skipped": _state.skipped,
            "errors": _state.errors,
            "message": _state.message,
            "cancel_requested": _state.cancel_requested,
            "paused": paused,
            "report": list(_state.report[-200:]),
        }


def request_raw_convert_cancel() -> dict[str, Any]:
    with _state.lock:
        if _state.status == "running":
            _state.cancel_requested = True
            _state.pause_requested = False
            _state.phase = "cancelling"
            _state.message = "Cancelling… finishing current file"
        else:
            _state.cancel_requested = True
            _state.message = "Cancel requested…"
    return get_raw_convert_status()


def request_raw_convert_pause() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No RAW convert job is running")
        if _state.cancel_requested:
            raise RuntimeError("RAW convert is cancelling")
        _state.pause_requested = True
        _state.phase = "paused"
        _state.message = "Paused — resume when ready (current file will finish)"
    return get_raw_convert_status()


def request_raw_convert_resume() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No RAW convert job is running")
        _state.pause_requested = False
        if _state.phase == "paused":
            _state.phase = "converting"
        _state.message = "Resuming RAW convert…"
    return get_raw_convert_status()


def _folder_for_date(dt: datetime, pattern: str) -> str:
    if pattern == "YYYY":
        return f"{dt.year:04d}"
    if pattern == "YYYY-MM":
        return f"{dt.year:04d}-{dt.month:02d}"
    return f"{dt.year:04d}/{dt.year:04d}-{dt.month:02d}"


def _unique_jpeg(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        candidate = dest.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _iter_raw_files(root: Path) -> list[Path]:
    found: list[Path] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not _should_skip_dir(entry.name):
                        stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            ext = Path(entry.name).suffix.lower()
            if ext in RAW_EXTENSIONS:
                found.append(Path(entry.path))
    return found


def preview_raw_convert() -> dict[str, Any]:
    """Count RAW files under PHOTO_ROOT (excludes quarantine)."""
    from app.services.jobs import get_busy_status

    busy = get_busy_status()
    if busy.get("busy") and busy.get("active"):
        # Preview is read-only walk — allow even when busy? Plan says preview.
        # Keep preview always allowed for UX.
        pass

    root = get_photo_root()
    files = _iter_raw_files(root)
    by_ext: dict[str, int] = {}
    sample: list[str] = []
    bytes_total = 0
    for p in files:
        ext = p.suffix.lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
        if len(sample) < 30:
            try:
                sample.append(str(p.relative_to(root)).replace("\\", "/"))
            except ValueError:
                sample.append(p.name)
        try:
            bytes_total += p.stat().st_size
        except OSError:
            pass
    return {
        "count": len(files),
        "bytes": bytes_total,
        "by_ext": by_ext,
        "sample": sample,
        "quarantine_dir": QUARANTINE_RAW_DIR_NAME,
    }


def start_raw_convert(
    *,
    confirm: bool = False,
    pattern: str = "YYYY/YYYY-MM",
    quarantine: bool = True,
) -> dict[str, Any]:
    if not confirm:
        raise ValueError("confirm=True is required")
    if pattern not in ("YYYY/YYYY-MM", "YYYY-MM", "YYYY"):
        raise ValueError("Invalid pattern")

    from app.services.jobs import JOB_RAW_CONVERT, assert_can_start

    assert_can_start(JOB_RAW_CONVERT)

    with _state.lock:
        if _state.status == "running":
            raise RuntimeError("A RAW convert job is already running")
        _state.status = "running"
        _state.phase = "starting"
        _state.total = 0
        _state.processed = 0
        _state.converted = 0
        _state.quarantined = 0
        _state.skipped = 0
        _state.errors = 0
        _state.message = "Starting RAW convert…"
        _state.report = []
        _state.cancel_requested = False
        _state.pause_requested = False

    thread = threading.Thread(
        target=_run_raw_convert,
        kwargs={"pattern": pattern, "quarantine": quarantine},
        daemon=True,
    )
    thread.start()
    return get_raw_convert_status()


def _bump_message() -> None:
    """Caller must hold _state.lock."""
    _state.message = (
        f"converted={_state.converted:,} quarantined={_state.quarantined:,} "
        f"skipped={_state.skipped:,} errors={_state.errors:,} · "
        f"{_state.processed:,}/{_state.total:,}"
    )


def _finish(status: str, message: str) -> None:
    from app.services.jobs import JOB_RAW_CONVERT, release_job

    with _state.lock:
        _state.status = status
        _state.phase = status
        _state.message = message
    release_job(JOB_RAW_CONVERT)


def _index_jpeg(root: Path, dest: Path, meta: dict[str, Any]) -> None:
    rel = dest.relative_to(root)
    folder = str(rel.parent).replace("\\", "/")
    if folder == ".":
        folder = ""
    stat = dest.stat()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM photos WHERE path = ?", (str(dest),)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE photos SET
                    rel_path=?, filename=?, folder=?, size=?, mtime=?,
                    width=?, height=?, exif_date=?, latitude=?, longitude=?,
                    media_type=?, duration=?, content_hash=?, phash=?, thumb_path=?,
                    indexed_at=datetime('now')
                WHERE id=?
                """,
                (
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
                    existing["id"],
                ),
            )
        else:
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


def _run_raw_convert(*, pattern: str, quarantine: bool) -> None:
    try:
        root = get_photo_root()
        with _state.lock:
            _state.phase = "scanning"
            _state.message = "Finding RAW files…"

        files = _iter_raw_files(root)
        with _state.lock:
            _state.total = len(files)
            _state.phase = "converting"
            _state.message = f"Converting {len(files):,} RAW files…"

        q_root = assert_under_root(root / QUARANTINE_RAW_DIR_NAME, root)

        def _announce_pause() -> None:
            with _state.lock:
                _state.phase = "paused"
                _state.message = "Paused — click Resume when ready"

        for src in files:
            from app.services.job_control import wait_while_paused

            if wait_while_paused(
                lock=_state.lock,
                is_cancelled=lambda: _state.cancel_requested,
                is_paused=lambda: _state.pause_requested,
                set_paused_ui=_announce_pause,
            ):
                _finish("cancelled", "RAW convert cancelled")
                return

            with _state.lock:
                if _state.cancel_requested:
                    _finish("cancelled", "RAW convert cancelled")
                    return

            try:
                src = assert_under_root(src, root)
            except ValueError as e:
                with _state.lock:
                    _state.errors += 1
                    _state.processed += 1
                    _state.report.append(
                        {"action": "error", "source": str(src), "error": str(e)}
                    )
                    _bump_message()
                continue

            try:
                if not src.exists():
                    with _state.lock:
                        _state.skipped += 1
                        _state.processed += 1
                        _bump_message()
                    continue
            except OSError as e:
                with _state.lock:
                    _state.errors += 1
                    _state.processed += 1
                    _state.report.append(
                        {
                            "action": "error",
                            "source": str(src.relative_to(root)).replace("\\", "/"),
                            "error": f"unreadable: {e}",
                        }
                    )
                    _bump_message()
                continue

            try:
                img, capture_dt = decode_raw_to_image(src)
                if capture_dt is None:
                    capture_dt = datetime.fromtimestamp(src.stat().st_mtime)
                folder = _folder_for_date(capture_dt, pattern)
                dest_dir = assert_under_root(root / folder, root)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique_jpeg(dest_dir / f"{src.stem}.jpg")
                dest = assert_under_root(dest, root)

                existing_jpeg = dest_dir / f"{src.stem}.jpg"
                if existing_jpeg.exists() and existing_jpeg != dest:
                    img.close()
                    with _state.lock:
                        _state.skipped += 1
                        _state.processed += 1
                        if _state.skipped <= 20 or _state.skipped % 50 == 0:
                            _state.report.append(
                                {
                                    "action": "skip",
                                    "reason": "jpeg already exists",
                                    "source": str(src.relative_to(root)).replace("\\", "/"),
                                    "existing": str(
                                        existing_jpeg.relative_to(root)
                                    ).replace("\\", "/"),
                                }
                            )
                        _bump_message()
                    if quarantine:
                        _quarantine_raw(root, q_root, src)
                        with _state.lock:
                            _state.quarantined += 1
                            _bump_message()
                    continue

                # Misnamed JPEGs: copy bytes to preserve EXIF. Real RAW: re-encode.
                is_jpeg_bytes = False
                try:
                    with src.open("rb") as f:
                        is_jpeg_bytes = f.read(3).startswith(b"\xff\xd8\xff")
                except OSError:
                    is_jpeg_bytes = False

                if is_jpeg_bytes:
                    shutil.copy2(str(src), str(dest))
                    img.close()
                else:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(dest, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
                    img.close()

                thumb_dest = THUMBS_DIR / "_raw" / f"{dest.stem}.jpg"
                meta = analyze_image(dest, thumb_dest)
                if not meta.get("exif_date"):
                    meta["exif_date"] = capture_dt.isoformat(sep=" ")
                _index_jpeg(root, dest, meta)

                if quarantine:
                    _quarantine_raw(root, q_root, src)
                    with _state.lock:
                        _state.quarantined += 1

                with _state.lock:
                    _state.converted += 1
                    _state.processed += 1
                    if _state.converted <= 40 or _state.converted % 25 == 0:
                        _state.report.append(
                            {
                                "action": "converted",
                                "source": str(src.relative_to(root)).replace("\\", "/"),
                                "dest": str(dest.relative_to(root)).replace("\\", "/"),
                            }
                        )
                    _bump_message()
            except Exception as e:
                with _state.lock:
                    _state.errors += 1
                    _state.processed += 1
                    try:
                        rel = str(src.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        rel = str(src)
                    _state.report.append(
                        {"action": "error", "source": rel, "error": str(e)[:300]}
                    )
                    _bump_message()

        with _state.lock:
            msg = (
                f"RAW convert done. converted={_state.converted} "
                f"quarantined={_state.quarantined} skipped={_state.skipped} "
                f"errors={_state.errors}"
            )
        _finish("completed", msg)
    except Exception as e:
        _finish("error", f"{e}\n{traceback.format_exc()}")


def _quarantine_raw(root: Path, q_root: Path, src: Path) -> None:
    rel = src.relative_to(root)
    dest = assert_under_root(q_root / rel, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{stem}_{n}{suffix}")
            n += 1
        dest = assert_under_root(dest, root)
    shutil.move(str(src), str(dest))
