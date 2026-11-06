"""Find and permanently delete junk / unreadable media under PHOTO_ROOT."""

from __future__ import annotations

import os
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, get_photo_root
from app.database import connect
from app.paths import assert_under_root
from app.services.hashing import analyze_image, analyze_video, is_video
from app.services.scanner import _should_skip_dir

_JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "autorun.inf"}
_MEDIA_EXT = {e.lower() for e in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS}


@dataclass
class CleanupState:
    status: str = "idle"
    phase: str = ""
    scanned: int = 0
    total: int = 0
    junk_count: int = 0
    corrupt_count: int = 0
    deleted: int = 0
    bytes_freed: int = 0
    errors: int = 0
    message: str = ""
    report: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = CleanupState()
_preview_cache: dict[str, Any] | None = None
_preview_lock = threading.Lock()


def get_cleanup_status() -> dict[str, Any]:
    with _state.lock:
        processed = _state.deleted + _state.errors
        return {
            "status": _state.status,
            "phase": _state.phase,
            "scanned": _state.scanned,
            "total": _state.total,
            "processed": processed,
            "junk_count": _state.junk_count,
            "corrupt_count": _state.corrupt_count,
            "deleted": _state.deleted,
            "bytes_freed": _state.bytes_freed,
            "errors": _state.errors,
            "message": _state.message,
            "report": list(_state.report[-300:]),
        }


def request_cancel() -> dict[str, Any]:
    with _state.lock:
        _state.cancel_requested = True
        _state.message = "Cancel requested…"
    return get_cleanup_status()


def preview_cleanup(*, check_corrupt: bool = True) -> dict[str, Any]:
    """
    Walk PHOTO_ROOT and list junk / unreadable unindexed media.
    Does not delete. Caches result for a subsequent start_cleanup.
    """
    from app.services.jobs import JOB_CLEANUP, assert_can_start, release_job

    # When called from a running cleanup job, the slot is already claimed —
    # do not release it when the nested preview returns.
    with _state.lock:
        claim_owned_by_running_job = _state.status == "running"
    assert_can_start(JOB_CLEANUP)

    try:
        return _preview_cleanup_body(check_corrupt=check_corrupt)
    finally:
        if not claim_owned_by_running_job:
            release_job(JOB_CLEANUP)


def _preview_cleanup_body(*, check_corrupt: bool = True) -> dict[str, Any]:
    root = get_photo_root()
    indexed: set[str] = set()
    with connect() as conn:
        for r in conn.execute("SELECT path FROM photos"):
            indexed.add(os.path.normpath(r["path"]))

    junk: list[dict[str, Any]] = []
    corrupt: list[dict[str, Any]] = []
    scanned = 0
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
            scanned += 1
            if scanned % 250 == 0:
                with _state.lock:
                    if _state.status == "running":
                        _state.scanned = scanned
                        _state.message = f"Scanning… {scanned:,} files checked"
            path = Path(entry.path)
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue

            name = entry.name
            lower = name.lower()
            if name.startswith("._") or lower in _JUNK_NAMES:
                junk.append(
                    {
                        "path": str(path),
                        "name": name,
                        "size": size,
                        "reason": "appledouble_or_os_junk",
                    }
                )
                continue

            if not check_corrupt:
                continue
            ext = path.suffix.lower()
            if ext not in _MEDIA_EXT:
                continue
            abs_path = os.path.normpath(entry.path)
            if abs_path in indexed:
                continue
            # Unindexed media — try decode; failures are corrupt/truncated/unreadable
            try:
                if size <= 0:
                    raise ValueError("empty file")
                thumb = Path(os.environ.get("TEMP", ".")) / f"mcp_cleanup_{os.getpid()}.jpg"
                if is_video(path):
                    analyze_video(path, thumb)
                else:
                    analyze_image(path, thumb)
                try:
                    thumb.unlink(missing_ok=True)
                except OSError:
                    pass
            except Exception:
                corrupt.append(
                    {
                        "path": str(path),
                        "name": name,
                        "size": size,
                        "reason": "unreadable_or_truncated",
                    }
                )

    junk_bytes = sum(i["size"] for i in junk)
    corrupt_bytes = sum(i["size"] for i in corrupt)
    result = {
        "scanned": scanned,
        "junk_count": len(junk),
        "junk_bytes": junk_bytes,
        "corrupt_count": len(corrupt),
        "corrupt_bytes": corrupt_bytes,
        "total_count": len(junk) + len(corrupt),
        "total_bytes": junk_bytes + corrupt_bytes,
        "junk_sample": junk[:100],
        "corrupt_sample": corrupt[:100],
        "check_corrupt": check_corrupt,
    }
    with _preview_lock:
        global _preview_cache
        _preview_cache = {
            "junk": junk,
            "corrupt": corrupt,
            "root": str(root),
            "check_corrupt": check_corrupt,
        }
    with _state.lock:
        if _state.status == "running":
            _state.scanned = scanned
            _state.message = f"Scan done — {len(junk):,} junk, {len(corrupt):,} corrupt"
    return result


def start_cleanup(
    *,
    confirm: bool = False,
    confirm_token: str | None = None,
    delete_junk: bool = True,
    delete_corrupt: bool = True,
    check_corrupt: bool = True,
) -> dict[str, Any]:
    from app.services.jobs import JOB_CLEANUP, assert_can_start
    from app.services.safety import CONFIRM_CLEANUP, require_confirm_token

    if not confirm:
        raise ValueError("confirm=true is required — this permanently deletes files")
    require_confirm_token(
        confirm_token, CONFIRM_CLEANUP, action="permanently delete junk/corrupt files"
    )
    if not delete_junk and not delete_corrupt:
        raise ValueError("Select at least one category to delete")

    assert_can_start(JOB_CLEANUP)

    with _state.lock:
        if _state.status == "running":
            raise RuntimeError("Cleanup already running")
        _state.status = "running"
        _state.phase = "starting"
        _state.scanned = 0
        _state.total = 0
        _state.junk_count = 0
        _state.corrupt_count = 0
        _state.deleted = 0
        _state.bytes_freed = 0
        _state.errors = 0
        _state.message = "Starting cleanup…"
        _state.report = []
        _state.cancel_requested = False

    thread = threading.Thread(
        target=_run_cleanup,
        kwargs={
            "delete_junk": delete_junk,
            "delete_corrupt": delete_corrupt,
            "check_corrupt": check_corrupt,
        },
        daemon=True,
    )
    thread.start()
    return get_cleanup_status()


def _run_cleanup(
    *,
    delete_junk: bool,
    delete_corrupt: bool,
    check_corrupt: bool,
) -> None:
    global _preview_cache
    try:
        root = get_photo_root()
        with _preview_lock:
            cache = _preview_cache

        # Refresh preview if missing or categories need corrupt list
        need_preview = cache is None or (
            delete_corrupt and check_corrupt and not cache.get("corrupt")
        )
        if need_preview:
            with _state.lock:
                _state.phase = "preview"
                _state.message = "Scanning for junk / unreadable files…"
            preview_cleanup(check_corrupt=check_corrupt and delete_corrupt)
            with _preview_lock:
                cache = _preview_cache

        assert cache is not None
        targets: list[tuple[str, dict[str, Any]]] = []
        if delete_junk:
            targets.extend(("junk", item) for item in cache["junk"])
        if delete_corrupt:
            targets.extend(("corrupt", item) for item in cache["corrupt"])

        with _state.lock:
            _state.junk_count = len(cache["junk"]) if delete_junk else 0
            _state.corrupt_count = len(cache["corrupt"]) if delete_corrupt else 0
            _state.total = len(targets)
            _state.phase = "deleting"
            _state.message = f"Deleting {len(targets):,} files…"

        for kind, item in targets:
            with _state.lock:
                if _state.cancel_requested:
                    _finish("cancelled", "Cleanup cancelled")
                    return
            path = Path(item["path"])
            try:
                safe = assert_under_root(path, root)
                if not safe.is_file():
                    continue
                size = safe.stat().st_size
                safe.unlink()
                with _state.lock:
                    _state.deleted += 1
                    _state.bytes_freed += size
                    _state.report.append(
                        {"action": "deleted", "kind": kind, "path": str(safe), "size": size}
                    )
                    _state.message = (
                        f"Deleted {_state.deleted:,}/{_state.total:,} · "
                        f"freed={_state.bytes_freed} · errors={_state.errors}"
                    )
            except Exception as e:
                with _state.lock:
                    _state.errors += 1
                    _state.report.append(
                        {
                            "action": "error",
                            "kind": kind,
                            "path": item.get("path"),
                            "error": str(e),
                        }
                    )
                    _state.message = (
                        f"Deleted {_state.deleted:,}/{_state.total:,} · "
                        f"errors={_state.errors}"
                    )

        with _state.lock:
            msg = (
                f"Cleanup done. deleted={_state.deleted} "
                f"freed={_state.bytes_freed} errors={_state.errors}"
            )
        _finish("completed", msg)
        with _preview_lock:
            _preview_cache = None
    except Exception as e:
        _finish("error", f"{e}\n{traceback.format_exc()}")


def _finish(status: str, message: str) -> None:
    from app.services.jobs import JOB_CLEANUP, release_job

    with _state.lock:
        _state.status = status
        _state.phase = status
        _state.message = message
    release_job(JOB_CLEANUP)
