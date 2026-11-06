"""iCloud inventory, space estimates, and rolling archive jobs."""

from __future__ import annotations

import shutil
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import (
    ICLOUD_ARCHIVE_BATCH_SIZE,
    ICLOUD_STAGING_DIR,
    ICLOUD_THUMBS_DIR,
    PHASH_THRESHOLD,
    THUMBS_DIR,
    ensure_data_dirs,
    get_photo_root,
)
from app.database import connect, get_setting, set_setting, rows_to_dicts
from app.paths import assert_under_root
from app.services import icloud_client
from app.services.hashing import (
    analyze_image,
    analyze_video,
    file_sha256,
    hamming_distance,
    is_image,
    is_video,
    open_image,
    compute_phash,
)

SETTING_INVENTORY_AT = "icloud_inventory_refreshed_at"
SETTING_INVENTORY_COUNT = "icloud_inventory_count"


@dataclass
class ArchiveState:
    job_id: int | None = None
    account_id: int | None = None
    status: str = "idle"
    phase: str = ""
    mode: str = ""
    total: int = 0
    processed: int = 0
    downloaded: int = 0
    imported: int = 0
    skipped_exact: int = 0
    skipped_near: int = 0
    verified: int = 0
    deleted_icloud: int = 0
    errors: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    batch_index: int = 0
    batch_count: int = 0
    message: str = ""
    cancel_requested: bool = False
    pause_requested: bool = False
    report: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class InventoryState:
    status: str = "idle"
    processed: int = 0
    total_hint: int = 0
    message: str = ""
    cancel_requested: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


_archive = ArchiveState()
_inventory = InventoryState()


def get_archive_status() -> dict[str, Any]:
    with _archive.lock:
        paused = bool(_archive.pause_requested) and _archive.status == "running"
        cancelling = bool(_archive.cancel_requested) and _archive.status == "running"
        return {
            "job_id": _archive.job_id,
            "account_id": _archive.account_id,
            "status": _archive.status,
            "phase": (
                "cancelling"
                if cancelling
                else "paused"
                if paused
                else _archive.phase
            ),
            "mode": _archive.mode,
            "total": _archive.total,
            "processed": _archive.processed,
            "downloaded": _archive.downloaded,
            "imported": _archive.imported,
            "skipped_exact": _archive.skipped_exact,
            "skipped_near": _archive.skipped_near,
            "verified": _archive.verified,
            "deleted_icloud": _archive.deleted_icloud,
            "errors": _archive.errors,
            "bytes_total": _archive.bytes_total,
            "bytes_done": _archive.bytes_done,
            "batch_index": _archive.batch_index,
            "batch_count": _archive.batch_count,
            "message": _archive.message,
            "cancel_requested": _archive.cancel_requested,
            "paused": paused and not cancelling,
            "report": list(_archive.report[-300:]),
        }


def get_inventory_status(*, lightweight: bool = False) -> dict[str, Any]:
    """
    Inventory job status for the active family account.
    lightweight=True skips DB count queries (for busy-poll while another job runs).
    """
    with _inventory.lock:
        processed = int(_inventory.processed or 0)
        total = int(_inventory.total_hint or 0)
        status = {
            "status": _inventory.status,
            "processed": processed,
            "total": total,
            "percent": (
                min(100, round(100 * processed / total)) if total > 0 else None
            ),
            "phase": (
                "cancelling"
                if _inventory.cancel_requested and _inventory.status == "running"
                else "indexing"
                if _inventory.status == "running"
                else _inventory.status or "idle"
            ),
            "unit": "assets",
            "message": _inventory.message,
            "cancel_requested": _inventory.cancel_requested,
        }
    if lightweight:
        return status
    account = icloud_client.get_account()
    account_id = int(account["id"])
    status["account_id"] = account_id
    status["account_label"] = account.get("label")
    status["refreshed_at"] = account.get("inventory_refreshed_at")
    status["cached_count"] = int(account.get("inventory_count") or 0)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS bytes
            FROM icloud_assets WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        by_lib = conn.execute(
            """
            SELECT library_key, COUNT(*) AS c, COALESCE(SUM(size), 0) AS bytes
            FROM icloud_assets
            WHERE account_id = ?
            GROUP BY library_key
            ORDER BY library_key
            """,
            (account_id,),
        ).fetchall()
    status["asset_count"] = int(row["c"] or 0)
    status["asset_bytes"] = int(row["bytes"] or 0)
    status["by_library"] = [
        {
            "library_key": r["library_key"] or "root",
            "count": int(r["c"] or 0),
            "bytes": int(r["bytes"] or 0),
            "shared": str(r["library_key"] or "").startswith("shared:"),
        }
        for r in by_lib
    ]
    # When idle/completed, prefer DB count as the known total for display
    if status["status"] != "running" and status["total"] <= 0 and status["asset_count"]:
        status["total"] = status["asset_count"]
        status["percent"] = 100 if status["status"] == "completed" else None
    return status


def request_inventory_cancel() -> dict[str, Any]:
    with _inventory.lock:
        _inventory.cancel_requested = True
    return get_inventory_status()


def request_archive_cancel() -> dict[str, Any]:
    with _archive.lock:
        _archive.cancel_requested = True
        _archive.pause_requested = False
        _archive.message = "Cancel requested…"
    return get_archive_status()


def request_archive_pause() -> dict[str, Any]:
    with _archive.lock:
        if _archive.status != "running":
            raise RuntimeError("No archive job is running")
        if _archive.cancel_requested:
            raise RuntimeError("Archive is cancelling")
        _archive.pause_requested = True
        _archive.phase = "paused"
        _archive.message = "Paused — resume when ready (current file will finish)"
    return get_archive_status()


def request_archive_resume() -> dict[str, Any]:
    with _archive.lock:
        if _archive.status != "running":
            raise RuntimeError("No archive job is running")
        _archive.pause_requested = False
        if _archive.phase == "paused":
            _archive.phase = "download"
        _archive.message = "Resuming archive…"
    return get_archive_status()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Invalid date: {value}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_cutoff(
    *,
    keep_years: float | None = None,
    keep_months: float | None = None,
    older_than: str | None = None,
) -> datetime | None:
    """Return UTC cutoff: assets with created_at < cutoff are archive candidates."""
    if older_than:
        return _parse_iso(older_than)
    now = datetime.now(timezone.utc)
    if keep_years is not None:
        days = float(keep_years) * 365.25
        return now - timedelta(days=days)
    if keep_months is not None:
        days = float(keep_months) * 30.4375
        return now - timedelta(days=days)
    return None


def _filter_sql(
    *,
    account_id: int,
    cutoff: datetime | None,
    range_start: datetime | None,
    range_end: datetime | None,
    asset_ids: list[str] | None,
    only_not_deleted: bool = True,
) -> tuple[str, list[Any]]:
    clauses: list[str] = ["account_id = ?"]
    params: list[Any] = [account_id]
    if only_not_deleted:
        clauses.append("deleted_from_icloud = 0")
    if asset_ids is not None:
        if not asset_ids:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" * len(asset_ids))
            clauses.append(f"asset_id IN ({placeholders})")
            params.extend(asset_ids)
    else:
        if cutoff is not None:
            clauses.append("created_at IS NOT NULL AND created_at < ?")
            params.append(cutoff.isoformat())
        if range_start is not None:
            clauses.append("created_at IS NOT NULL AND created_at >= ?")
            params.append(range_start.isoformat())
        if range_end is not None:
            clauses.append("created_at IS NOT NULL AND created_at < ?")
            params.append(range_end.isoformat())
    where = " AND ".join(clauses) if clauses else "1 = 1"
    return where, params


def estimate_archive(
    *,
    keep_years: float | None = None,
    keep_months: float | None = None,
    older_than: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    asset_ids: list[str] | None = None,
) -> dict[str, Any]:
    cutoff = resolve_cutoff(
        keep_years=keep_years, keep_months=keep_months, older_than=older_than
    )
    start = _parse_iso(range_start) if range_start else None
    end = _parse_iso(range_end) if range_end else None
    if asset_ids is None and cutoff is None and start is None and end is None:
        raise ValueError(
            "Provide keep_years / keep_months / older_than, a date range, or asset_ids"
        )

    where, params = _filter_sql(
        account_id=icloud_client.get_active_account_id(),
        cutoff=cutoff if asset_ids is None else None,
        range_start=start if asset_ids is None else None,
        range_end=end if asset_ids is None else None,
        asset_ids=asset_ids,
    )
    account_id = icloud_client.get_active_account_id()
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS count,
                COALESCE(SUM(size), 0) AS bytes,
                SUM(CASE WHEN item_type = 'video' THEN 1 ELSE 0 END) AS videos,
                SUM(CASE WHEN item_type != 'video' THEN 1 ELSE 0 END) AS photos,
                COALESCE(SUM(CASE WHEN item_type = 'video' THEN size ELSE 0 END), 0) AS video_bytes,
                COALESCE(SUM(CASE WHEN item_type != 'video' THEN size ELSE 0 END), 0) AS photo_bytes
            FROM icloud_assets
            WHERE {where}
            """,
            params,
        ).fetchone()
        total_row = conn.execute(
            """
            SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS b
            FROM icloud_assets
            WHERE deleted_from_icloud = 0 AND account_id = ?
            """,
            (account_id,),
        ).fetchone()

    count = int(row["count"] or 0)
    bytes_total = int(row["bytes"] or 0)
    return {
        "count": count,
        "bytes": bytes_total,
        "photos": int(row["photos"] or 0),
        "videos": int(row["videos"] or 0),
        "photo_bytes": int(row["photo_bytes"] or 0),
        "video_bytes": int(row["video_bytes"] or 0),
        "cutoff": cutoff.isoformat() if cutoff else None,
        "range_start": start.isoformat() if start else None,
        "range_end": end.isoformat() if end else None,
        "inventory_total": int(total_row["c"] or 0),
        "inventory_bytes": int(total_row["b"] or 0),
        "batch_size": ICLOUD_ARCHIVE_BATCH_SIZE,
        "estimated_batches": (count + ICLOUD_ARCHIVE_BATCH_SIZE - 1)
        // ICLOUD_ARCHIVE_BATCH_SIZE
        if count
        else 0,
        "refreshed_at": icloud_client.get_account().get("inventory_refreshed_at"),
        "account_id": account_id,
        "account_label": icloud_client.get_account().get("label"),
    }


def list_matching_assets(
    *,
    keep_years: float | None = None,
    keep_months: float | None = None,
    older_than: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    asset_ids: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    cutoff = resolve_cutoff(
        keep_years=keep_years, keep_months=keep_months, older_than=older_than
    )
    start = _parse_iso(range_start) if range_start else None
    end = _parse_iso(range_end) if range_end else None
    where, params = _filter_sql(
        account_id=icloud_client.get_active_account_id(),
        cutoff=cutoff if asset_ids is None else None,
        range_start=start if asset_ids is None else None,
        range_end=end if asset_ids is None else None,
        asset_ids=asset_ids,
    )
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM icloud_assets WHERE {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT account_id, asset_id, filename, created_at, size, item_type, is_live_photo,
                   library_key, archived_locally, deleted_from_icloud, local_rel_path
            FROM icloud_assets
            WHERE {where}
            ORDER BY (created_at IS NULL), created_at ASC, asset_id
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "assets": rows_to_dicts(list(rows)),
        "cutoff": cutoff.isoformat() if cutoff else None,
        "account_id": icloud_client.get_active_account_id(),
    }


def list_largest_assets(
    *,
    keep_years: float | None = None,
    keep_months: float | None = None,
    older_than: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    entire_inventory: bool = False,
    media: str = "all",
    limit: int = 50,
) -> dict[str, Any]:
    """
    Rank cached inventory by size (largest first) for archive prioritization.
    Default scope respects keep window / date range; entire_inventory skips dates.
    """
    media_key = (media or "all").strip().lower()
    if media_key not in ("all", "video", "photo"):
        raise ValueError("media must be all, video, or photo")
    limit = max(1, min(int(limit), 200))

    cutoff = None
    start = None
    end = None
    if not entire_inventory:
        cutoff = resolve_cutoff(
            keep_years=keep_years, keep_months=keep_months, older_than=older_than
        )
        start = _parse_iso(range_start) if range_start else None
        end = _parse_iso(range_end) if range_end else None
        if cutoff is None and start is None and end is None:
            raise ValueError(
                "Provide keep_years / keep_months / older_than, a date range, "
                "or set entire_inventory=true"
            )

    account_id = icloud_client.get_active_account_id()
    where, params = _filter_sql(
        account_id=account_id,
        cutoff=cutoff,
        range_start=start,
        range_end=end,
        asset_ids=None,
    )
    if media_key == "video":
        where = f"{where} AND item_type = ?"
        params.append("video")
    elif media_key == "photo":
        where = f"{where} AND item_type != ?"
        params.append("video")

    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM icloud_assets WHERE {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT account_id, asset_id, filename, created_at, size, item_type, is_live_photo,
                   library_key, archived_locally, deleted_from_icloud, local_rel_path
            FROM icloud_assets
            WHERE {where}
            ORDER BY size DESC, asset_id
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()

    assets = rows_to_dicts(list(rows))
    bytes_listed = sum(int(a.get("size") or 0) for a in assets)
    return {
        "assets": assets,
        "total_matching": int(total),
        "bytes_listed": bytes_listed,
        "limit": limit,
        "media": media_key,
        "entire_inventory": bool(entire_inventory),
        "cutoff": cutoff.isoformat() if cutoff else None,
        "range_start": start.isoformat() if start else None,
        "range_end": end.isoformat() if end else None,
        "account_id": account_id,
        "account_label": icloud_client.get_account().get("label"),
    }


def start_inventory_refresh() -> dict[str, Any]:
    from app.services.jobs import JOB_INVENTORY, assert_can_start, release_job

    assert_can_start(JOB_INVENTORY)

    account = icloud_client.get_account()
    with _inventory.lock:
        if _inventory.status == "running":
            raise RuntimeError("Inventory refresh already running")
        prior = int(account.get("inventory_count") or 0)
        _inventory.status = "running"
        _inventory.processed = 0
        _inventory.total_hint = prior
        _inventory.message = f"Connecting to iCloud Photos ({account.get('label')})…"
        _inventory.cancel_requested = False

    try:
        icloud_client.ensure_icloud_gitignore()
        # Validate auth before starting thread
        icloud_client.get_api()
    except Exception:
        with _inventory.lock:
            _inventory.status = "idle"
            _inventory.message = ""
        release_job(JOB_INVENTORY)
        raise

    thread = threading.Thread(target=_run_inventory_refresh, daemon=True)
    thread.start()
    return get_inventory_status()


def _run_inventory_refresh() -> None:
    from app.services.jobs import JOB_INVENTORY, release_job

    try:
        account = icloud_client.get_account()
        account_id = int(account["id"])
        label = account.get("label") or "account"
        seen = 0
        batch: list[tuple] = []
        lib_counts: dict[str, int] = {}
        current_lib: str | None = None

        libs = icloud_client.list_photo_libraries(include_shared=True)
        with _inventory.lock:
            shared_n = sum(1 for k, _ in libs if k.startswith("shared:"))
            _inventory.message = (
                f"{label}: counting assets in {len(libs)} libraries "
                f"({shared_n} Shared Photo Library zone(s))…"
            )

        # Real album lengths give a meaningful % even on the first refresh
        try:
            live_total = icloud_client.estimate_library_asset_count(include_shared=True)
        except Exception:
            live_total = 0
        with _inventory.lock:
            prior = int(_inventory.total_hint or 0)
            hint = max(prior, live_total)
            if hint > 0:
                _inventory.total_hint = hint
                _inventory.message = (
                    f"{label}: indexing up to {hint:,} assets from iCloud…"
                )
            else:
                _inventory.message = f"{label}: indexing assets (total unknown yet)…"

        def _bump(seen_n: int, *, force: bool = False) -> None:
            with _inventory.lock:
                _inventory.processed = seen_n
                # If we discover more than the estimate, grow the denominator
                if _inventory.total_hint and seen_n > _inventory.total_hint:
                    _inventory.total_hint = seen_n
                if _inventory.total_hint:
                    pct = min(100, round(100 * seen_n / _inventory.total_hint))
                    _inventory.message = (
                        f"{label}: indexed {seen_n:,}/{_inventory.total_hint:,} "
                        f"assets ({pct}%)…"
                    )
                else:
                    _inventory.message = f"{label}: indexed {seen_n:,} assets…"

        for library_key, photo in icloud_client.iter_library_assets(include_shared=True):
            with _inventory.lock:
                if _inventory.cancel_requested:
                    _inventory.status = "cancelled"
                    _inventory.message = f"Cancelled after {seen:,} assets"
                    release_job(JOB_INVENTORY)
                    return
            if library_key != current_lib:
                current_lib = library_key
                lib_label = (
                    "Shared Photo Library"
                    if library_key.startswith("shared:")
                    else "Personal library"
                )
                with _inventory.lock:
                    _inventory.message = (
                        f"{label} · {lib_label} — {seen:,} indexed so far…"
                    )
            row = icloud_client.asset_to_row(photo, library_key=library_key)
            batch.append(
                (
                    account_id,
                    row["asset_id"],
                    row["filename"],
                    row["created_at"],
                    row["size"],
                    row["item_type"],
                    row["is_live_photo"],
                    row["library_key"],
                )
            )
            lib_counts[library_key] = lib_counts.get(library_key, 0) + 1
            seen += 1
            if len(batch) >= 200:
                _flush_inventory_batch(batch)
                batch = []
                _bump(seen, force=True)
            elif seen % 25 == 0:
                _bump(seen)

        if batch:
            _flush_inventory_batch(batch)

        now_iso = datetime.now(timezone.utc).isoformat()
        with connect() as conn:
            conn.execute(
                """
                UPDATE icloud_accounts
                SET inventory_refreshed_at = ?, inventory_count = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (now_iso, seen, account_id),
            )
        # Legacy global keys (compat)
        set_setting(SETTING_INVENTORY_AT, now_iso)
        set_setting(SETTING_INVENTORY_COUNT, str(seen))
        shared_total = sum(n for k, n in lib_counts.items() if k.startswith("shared:"))
        with _inventory.lock:
            _inventory.processed = seen
            _inventory.total_hint = seen
            _inventory.status = "completed"
            if shared_total:
                _inventory.message = (
                    f"{label}: inventory ready — {seen:,} assets "
                    f"({shared_total:,} from Shared Photo Library)"
                )
            else:
                _inventory.message = f"{label}: inventory ready — {seen:,} assets"
        release_job(JOB_INVENTORY)
    except Exception as e:
        with _inventory.lock:
            _inventory.status = "error"
            _inventory.message = f"{e}"
        release_job(JOB_INVENTORY)
        logger_exc = traceback.format_exc()
        print(logger_exc)


def _flush_inventory_batch(batch: list[tuple]) -> None:
    from app.database import run_with_retry

    def _write() -> None:
        with connect() as conn:
            conn.executemany(
                """
                INSERT INTO icloud_assets(
                    account_id, asset_id, filename, created_at, size, item_type,
                    is_live_photo, library_key, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(account_id, asset_id) DO UPDATE SET
                    filename=excluded.filename,
                    created_at=excluded.created_at,
                    size=excluded.size,
                    item_type=excluded.item_type,
                    is_live_photo=excluded.is_live_photo,
                    library_key=excluded.library_key,
                    updated_at=datetime('now')
                """,
                batch,
            )

    run_with_retry(_write, label="inventory flush")


def start_archive(
    *,
    keep_years: float | None = None,
    keep_months: float | None = None,
    older_than: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    asset_ids: list[str] | None = None,
    delete_after: bool = False,
    dest_folder: str = "FromiCloud",
    check_near: bool = True,
    confirm_delete: bool = False,
    confirm_token: str | None = None,
) -> dict[str, Any]:
    from app.services.jobs import JOB_ARCHIVE, assert_can_start, release_job
    from app.services.safety import CONFIRM_DELETE_ICLOUD, require_confirm_token

    assert_can_start(JOB_ARCHIVE)

    try:
        if delete_after and not confirm_delete:
            raise ValueError(
                "Set confirm_delete=true to remove assets from iCloud after they are verified on the WD drive"
            )
        if delete_after:
            require_confirm_token(
                confirm_token,
                CONFIRM_DELETE_ICLOUD,
                action="remove verified assets from iCloud",
            )

        account = icloud_client.get_account()
        account_id = int(account["id"])
        if dest_folder.strip() in ("", "FromiCloud"):
            dest_folder = f"FromiCloud/{account.get('label') or 'Unknown'}"

        estimate = estimate_archive(
            keep_years=keep_years,
            keep_months=keep_months,
            older_than=older_than,
            range_start=range_start,
            range_end=range_end,
            asset_ids=asset_ids,
        )
        if estimate["count"] == 0:
            raise ValueError(
                "No matching iCloud assets to archive — refresh inventory or adjust filters"
            )

        get_photo_root()  # ensure configured
        icloud_client.get_api()

        cutoff = estimate.get("cutoff")
        mode = (
            "selected"
            if asset_ids
            else ("range" if range_start or range_end else "older_than")
        )

        with _archive.lock:
            if _archive.status == "running":
                raise RuntimeError("An archive job is already running")
            _archive.job_id = None
            _archive.status = "running"
            _archive.phase = "starting"
            _archive.mode = mode
            _archive.account_id = account_id
            _archive.cancel_requested = False
            _archive.pause_requested = False
            _archive.total = estimate["count"]
            _archive.processed = 0
            _archive.downloaded = 0
            _archive.imported = 0
            _archive.skipped_exact = 0
            _archive.skipped_near = 0
            _archive.verified = 0
            _archive.deleted_icloud = 0
            _archive.errors = 0
            _archive.bytes_total = estimate["bytes"]
            _archive.bytes_done = 0
            _archive.batch_index = 0
            _archive.batch_count = estimate["estimated_batches"]
            _archive.message = "Starting archive…"
            _archive.report = []

        with connect() as conn:
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(icloud_archive_jobs)").fetchall()
            }
            if "account_id" in cols:
                cur = conn.execute(
                    """
                    INSERT INTO icloud_archive_jobs(
                        account_id, status, phase, mode, cutoff, range_start, range_end,
                        delete_after, total, bytes_total, batch_count, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        "running",
                        "starting",
                        mode,
                        cutoff,
                        estimate.get("range_start"),
                        estimate.get("range_end"),
                        1 if delete_after else 0,
                        estimate["count"],
                        estimate["bytes"],
                        estimate["estimated_batches"],
                        "Starting archive…",
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO icloud_archive_jobs(
                        status, phase, mode, cutoff, range_start, range_end, delete_after,
                        total, bytes_total, batch_count, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "running",
                        "starting",
                        mode,
                        cutoff,
                        estimate.get("range_start"),
                        estimate.get("range_end"),
                        1 if delete_after else 0,
                        estimate["count"],
                        estimate["bytes"],
                        estimate["estimated_batches"],
                        "Starting archive…",
                    ),
                )
            job_id = int(cur.lastrowid)

        with _archive.lock:
            _archive.job_id = job_id
    except Exception:
        with _archive.lock:
            started = _archive.status == "running" and _archive.job_id is not None
        if not started:
            release_job(JOB_ARCHIVE)
            with _archive.lock:
                if _archive.status == "running":
                    _archive.status = "idle"
                    _archive.phase = ""
                    _archive.account_id = None
        raise

    thread = threading.Thread(
        target=_run_archive,
        kwargs={
            "job_id": job_id,
            "keep_years": keep_years,
            "keep_months": keep_months,
            "older_than": older_than,
            "range_start": range_start,
            "range_end": range_end,
            "asset_ids": asset_ids,
            "delete_after": delete_after,
            "dest_folder": dest_folder,
            "check_near": check_near,
        },
        daemon=True,
    )
    thread.start()
    return get_archive_status()


def _selected_asset_rows(
    *,
    keep_years: float | None,
    keep_months: float | None,
    older_than: str | None,
    range_start: str | None,
    range_end: str | None,
    asset_ids: list[str] | None,
) -> list[dict[str, Any]]:
    data = list_matching_assets(
        keep_years=keep_years,
        keep_months=keep_months,
        older_than=older_than,
        range_start=range_start,
        range_end=range_end,
        asset_ids=asset_ids,
        limit=1_000_000,
        offset=0,
    )
    return data["assets"]


def _run_archive(
    *,
    job_id: int,
    keep_years: float | None,
    keep_months: float | None,
    older_than: str | None,
    range_start: str | None,
    range_end: str | None,
    asset_ids: list[str] | None,
    delete_after: bool,
    dest_folder: str,
    check_near: bool,
) -> None:
    ensure_data_dirs()
    staging_root = ICLOUD_STAGING_DIR / str(job_id)
    staging_root.mkdir(parents=True, exist_ok=True)

    try:
        assets = _selected_asset_rows(
            keep_years=keep_years,
            keep_months=keep_months,
            older_than=older_than,
            range_start=range_start,
            range_end=range_end,
            asset_ids=asset_ids,
        )
        batch_size = ICLOUD_ARCHIVE_BATCH_SIZE
        batches = [
            assets[i : i + batch_size] for i in range(0, len(assets), batch_size)
        ]
        with _archive.lock:
            _archive.batch_count = len(batches)
            _archive.total = len(assets)
            _archive.bytes_total = sum(int(a.get("size") or 0) for a in assets)

        # Build id -> live photo object map by scanning private + Shared libraries once
        _set_phase(job_id, "indexing", "Building live asset map from iCloud…")
        wanted = {a["asset_id"] for a in assets}
        live_map: dict[str, Any] = {}
        for _library_key, photo in icloud_client.iter_library_assets(include_shared=True):
            with _archive.lock:
                if _archive.cancel_requested:
                    _finish_archive(job_id, "cancelled", "Archive cancelled during indexing")
                    return
            pid = str(photo.id)
            if pid in wanted and pid not in live_map:
                live_map[pid] = photo
                if len(live_map) >= len(wanted):
                    break

        missing = wanted - set(live_map)
        if missing:
            # Fallback: direct lookups (helps when Shared zone scan is partial)
            for asset in assets:
                aid = asset["asset_id"]
                if aid not in missing:
                    continue
                photo = icloud_client.find_asset(
                    aid, library_key=asset.get("library_key")
                )
                if photo is not None:
                    live_map[aid] = photo
                    missing.discard(aid)
            if missing:
                with _archive.lock:
                    _archive.errors += len(missing)
                    _archive.report.append(
                        {
                            "action": "missing_in_icloud",
                            "count": len(missing),
                            "sample": list(missing)[:10],
                        }
                    )

        for bi, batch in enumerate(batches, start=1):
            with _archive.lock:
                if _archive.cancel_requested:
                    _finish_archive(job_id, "cancelled", "Archive cancelled")
                    return
                _archive.batch_index = bi

            batch_dir = staging_root / f"batch_{bi:04d}"
            batch_dir.mkdir(parents=True, exist_ok=True)

            # --- download ---
            _set_phase(
                job_id,
                "download",
                f"Downloading batch {bi}/{len(batches)} ({len(batch)} assets)…",
            )
            downloaded_paths: list[tuple[dict[str, Any], Path, Any]] = []
            from app.services.job_control import wait_while_paused

            for asset in batch:
                with _archive.lock:
                    if _archive.cancel_requested:
                        _finish_archive(job_id, "cancelled", "Archive cancelled")
                        return

                def _announce_pause() -> None:
                    with _archive.lock:
                        _archive.phase = "paused"
                        _archive.message = (
                            "Paused — resume when ready (current file will finish)"
                        )

                if wait_while_paused(
                    lock=_archive.lock,
                    is_cancelled=lambda: _archive.cancel_requested,
                    is_paused=lambda: _archive.pause_requested,
                    set_paused_ui=_announce_pause,
                ):
                    _finish_archive(job_id, "cancelled", "Archive cancelled")
                    return

                photo = live_map.get(asset["asset_id"])
                if photo is None:
                    with _archive.lock:
                        _archive.processed += 1
                        _archive.errors += 1
                        _archive.report.append(
                            {
                                "action": "error",
                                "asset_id": asset["asset_id"],
                                "error": "Asset not found in live iCloud library",
                            }
                        )
                    continue
                try:
                    fname = icloud_client.safe_filename(
                        asset.get("filename") or getattr(photo, "filename", ""),
                        asset["asset_id"],
                    )
                    dest = batch_dir / f"{asset['asset_id'][:12]}_{fname}"
                    written = icloud_client.download_asset_to_path(photo, dest)
                    downloaded_paths.append((asset, dest, photo))
                    with _archive.lock:
                        _archive.downloaded += 1
                        _archive.bytes_done += written
                        _archive.report.append(
                            {
                                "action": "downloaded",
                                "asset_id": asset["asset_id"],
                                "bytes": written,
                                "file": dest.name,
                            }
                        )
                except Exception as e:
                    with _archive.lock:
                        _archive.errors += 1
                        _archive.processed += 1
                        _archive.report.append(
                            {
                                "action": "error",
                                "asset_id": asset["asset_id"],
                                "phase": "download",
                                "error": str(e),
                            }
                        )

            # --- import ---
            _set_phase(
                job_id,
                "import",
                f"Importing batch {bi}/{len(batches)} onto WD…",
            )
            hash_cache, phash_cache = _load_dedupe_caches()
            imported_ok: list[tuple[dict[str, Any], Path, str, Any]] = []
            for asset, path, photo in downloaded_paths:
                with _archive.lock:
                    if _archive.cancel_requested:
                        _finish_archive(job_id, "cancelled", "Archive cancelled")
                        return
                try:
                    result = _import_one_file(
                        path,
                        dest_folder=dest_folder,
                        check_near=check_near,
                        created_at=asset.get("created_at"),
                        known_hashes=hash_cache,
                        library_phashes=phash_cache,
                    )
                    action = result["action"]
                    with _archive.lock:
                        if action == "copied":
                            _archive.imported += 1
                        elif action == "skip_exact":
                            _archive.skipped_exact += 1
                        elif action == "skip_near":
                            _archive.skipped_near += 1
                        _archive.report.append(
                            {
                                "action": action,
                                "asset_id": asset["asset_id"],
                                "rel_path": result.get("rel_path"),
                                "match": result.get("match"),
                            }
                        )
                    if action in ("copied", "skip_exact"):
                        # Exact skip still counts as verified-on-WD (already there)
                        rel = result.get("rel_path") or result.get("match")
                        if rel:
                            imported_ok.append((asset, path, rel, photo))
                except Exception as e:
                    with _archive.lock:
                        _archive.errors += 1
                        _archive.report.append(
                            {
                                "action": "error",
                                "asset_id": asset["asset_id"],
                                "phase": "import",
                                "error": str(e),
                            }
                        )

            # --- verify ---
            _set_phase(
                job_id,
                "verify",
                f"Verifying batch {bi}/{len(batches)} on WD…",
            )
            verified: list[tuple[dict[str, Any], Any]] = []
            root = get_photo_root()
            for asset, _path, rel, photo in imported_ok:
                try:
                    local = assert_under_root(root / rel, root)
                    if not local.exists() or local.stat().st_size <= 0:
                        raise RuntimeError(f"Missing or empty on WD: {rel}")
                    with connect() as conn:
                        conn.execute(
                            """
                            UPDATE icloud_assets
                            SET archived_locally = 1, local_rel_path = ?, updated_at = datetime('now')
                            WHERE account_id = ? AND asset_id = ?
                            """,
                            (
                                rel.replace("\\", "/"),
                                int(asset.get("account_id") or icloud_client.get_active_account_id()),
                                asset["asset_id"],
                            ),
                        )
                    verified.append((asset, photo))
                    with _archive.lock:
                        _archive.verified += 1
                except Exception as e:
                    with _archive.lock:
                        _archive.errors += 1
                        _archive.report.append(
                            {
                                "action": "error",
                                "asset_id": asset["asset_id"],
                                "phase": "verify",
                                "error": str(e),
                            }
                        )

            # --- delete from iCloud ---
            if delete_after and verified:
                _set_phase(
                    job_id,
                    "remove_from_icloud",
                    f"Removing batch {bi}/{len(batches)} from iCloud (Recently Deleted)…",
                )
                delete_i = 0
                for asset, photo in verified:
                    with _archive.lock:
                        if _archive.cancel_requested:
                            _finish_archive(
                                job_id,
                                "cancelled",
                                "Cancelled — some assets may already be in Recently Deleted",
                            )
                            return
                    try:
                        icloud_client.delete_asset(photo)
                        with connect() as conn:
                            conn.execute(
                                """
                                UPDATE icloud_assets
                                SET deleted_from_icloud = 1, updated_at = datetime('now')
                                WHERE account_id = ? AND asset_id = ?
                                """,
                                (
                                    int(asset.get("account_id") or icloud_client.get_active_account_id()),
                                    asset["asset_id"],
                                ),
                            )
                        delete_i += 1
                        with _archive.lock:
                            _archive.deleted_icloud += 1
                            # Keep overall totals; advance processed through this batch's deletes
                            base = (bi - 1) * batch_size
                            _archive.processed = min(
                                _archive.total, base + delete_i
                            )
                            _archive.message = (
                                f"Removing from iCloud… "
                                f"{_archive.deleted_icloud} removed · "
                                f"batch {bi}/{len(batches)} "
                                f"({delete_i}/{len(verified)})"
                            )
                            _archive.report.append(
                                {
                                    "action": "deleted_icloud",
                                    "asset_id": asset["asset_id"],
                                }
                            )
                    except Exception as e:
                        delete_i += 1
                        with _archive.lock:
                            _archive.errors += 1
                            base = (bi - 1) * batch_size
                            _archive.processed = min(
                                _archive.total, base + delete_i
                            )
                            _archive.report.append(
                                {
                                    "action": "error",
                                    "asset_id": asset["asset_id"],
                                    "phase": "remove_from_icloud",
                                    "error": str(e),
                                }
                            )

            with _archive.lock:
                # Better processed: count assets handled in finished batches
                _archive.processed = min(bi * batch_size, _archive.total)

            # Clear staging for this batch to free disk
            try:
                shutil.rmtree(batch_dir, ignore_errors=True)
            except Exception:
                pass

        with _archive.lock:
            msg = (
                f"Archive done. imported={_archive.imported} "
                f"exact_skip={_archive.skipped_exact} near_skip={_archive.skipped_near} "
                f"verified={_archive.verified} deleted_icloud={_archive.deleted_icloud} "
                f"errors={_archive.errors}"
            )
        _finish_archive(job_id, "completed", msg)
    except Exception as e:
        _finish_archive(job_id, "error", f"{e}\n{traceback.format_exc()}")
    finally:
        try:
            shutil.rmtree(staging_root, ignore_errors=True)
        except Exception:
            pass


def _set_phase(job_id: int, phase: str, message: str) -> None:
    with _archive.lock:
        _archive.phase = phase
        _archive.message = message
        snap = get_archive_status()
    with connect() as conn:
        conn.execute(
            """
            UPDATE icloud_archive_jobs SET
                phase=?, message=?,
                processed=?, downloaded=?, imported=?, skipped_exact=?, skipped_near=?,
                verified=?, deleted_icloud=?, errors=?, bytes_done=?,
                batch_index=?, batch_count=?
            WHERE id=?
            """,
            (
                phase,
                message[:2000],
                snap["processed"],
                snap["downloaded"],
                snap["imported"],
                snap["skipped_exact"],
                snap["skipped_near"],
                snap["verified"],
                snap["deleted_icloud"],
                snap["errors"],
                snap["bytes_done"],
                snap["batch_index"],
                snap["batch_count"],
                job_id,
            ),
        )


def _finish_archive(job_id: int, status: str, message: str) -> None:
    from app.services.jobs import JOB_ARCHIVE, release_job

    with _archive.lock:
        _archive.status = status
        _archive.phase = status
        _archive.message = message
        snap = {
            "total": _archive.total,
            "processed": _archive.processed,
            "downloaded": _archive.downloaded,
            "imported": _archive.imported,
            "skipped_exact": _archive.skipped_exact,
            "skipped_near": _archive.skipped_near,
            "verified": _archive.verified,
            "deleted_icloud": _archive.deleted_icloud,
            "errors": _archive.errors,
            "bytes_done": _archive.bytes_done,
            "batch_index": _archive.batch_index,
            "batch_count": _archive.batch_count,
        }
    with connect() as conn:
        conn.execute(
            """
            UPDATE icloud_archive_jobs SET
                status=?, phase=?, message=?, finished_at=datetime('now'),
                total=?, processed=?, downloaded=?, imported=?, skipped_exact=?,
                skipped_near=?, verified=?, deleted_icloud=?, errors=?,
                bytes_done=?, batch_index=?, batch_count=?
            WHERE id=?
            """,
            (
                status,
                status,
                message[:2000],
                snap["total"],
                snap["processed"],
                snap["downloaded"],
                snap["imported"],
                snap["skipped_exact"],
                snap["skipped_near"],
                snap["verified"],
                snap["deleted_icloud"],
                snap["errors"],
                snap["bytes_done"],
                snap["batch_index"],
                snap["batch_count"],
                job_id,
            ),
        )
    release_job(JOB_ARCHIVE)


def _year_subfolder(created_at: str | None) -> str:
    if created_at:
        try:
            dt = _parse_iso(created_at)
            if dt:
                return str(dt.year)
        except ValueError:
            pass
    return "unknown"


def _load_dedupe_caches() -> tuple[dict[str, str], list[tuple[str, str, str | None]]]:
    """Return (content_hash -> rel_path, [(phash, rel_path, content_hash), ...])."""
    with connect() as conn:
        hash_rows = conn.execute(
            "SELECT content_hash, rel_path FROM photos WHERE content_hash IS NOT NULL"
        ).fetchall()
        phash_rows = conn.execute(
            "SELECT phash, rel_path, content_hash FROM photos WHERE phash IS NOT NULL"
        ).fetchall()
    known = {r["content_hash"]: r["rel_path"] for r in hash_rows}
    phashes = [(r["phash"], r["rel_path"], r["content_hash"]) for r in phash_rows]
    return known, phashes


def _import_one_file(
    path: Path,
    *,
    dest_folder: str,
    check_near: bool,
    created_at: str | None,
    known_hashes: dict[str, str] | None = None,
    library_phashes: list[tuple[str, str, str | None]] | None = None,
) -> dict[str, Any]:
    """Copy one media file into PHOTO_ROOT with dedupe. Returns action dict."""
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    if not (is_image(path) or is_video(path)):
        raise ValueError(f"Unsupported media type: {path.suffix}")

    root = get_photo_root()
    dest_folder_clean = dest_folder.strip().replace("\\", "/").strip("/") or "FromiCloud"
    year = _year_subfolder(created_at)
    dest_root = assert_under_root(root / dest_folder_clean / year, root)
    dest_root.mkdir(parents=True, exist_ok=True)

    thr = PHASH_THRESHOLD
    if known_hashes is None or library_phashes is None:
        known_hashes, library_phashes = _load_dedupe_caches()

    ch: str | None
    if is_image(path) or path.stat().st_size <= 50 * 1024 * 1024:
        ch = file_sha256(path)
    else:
        ch = None

    if ch and ch in known_hashes:
        return {
            "action": "skip_exact",
            "match": known_hashes[ch],
            "rel_path": known_hashes[ch],
        }

    near_match = None
    if check_near and is_image(path):
        with open_image(path) as img:
            ph = compute_phash(img)
        for lib_ph, rel_path, lib_ch in library_phashes:
            if hamming_distance(ph, lib_ph) <= thr:
                if ch and lib_ch == ch:
                    continue
                near_match = rel_path
                break

    if near_match:
        return {"action": "skip_near", "match": near_match}

    dest = dest_root / path.name
    name = path.name
    if "_" in name and len(name.split("_", 1)[0]) == 12:
        name = name.split("_", 1)[1]
        dest = dest_root / name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = dest_root / f"{stem}_{n}{suffix}"
            n += 1

    shutil.copy2(str(path), str(dest))

    if is_video(dest):
        tmp_hash = (
            file_sha256(dest)
            if dest.stat().st_size <= 50 * 1024 * 1024
            else file_sha256(data=f"{dest.name}|{dest.stat().st_size}".encode())
        )
        thumb_dest = THUMBS_DIR / tmp_hash[:2] / f"{tmp_hash}.jpg"
        meta = analyze_video(dest, thumb_dest)
        if meta["content_hash"] != tmp_hash:
            final_thumb = (
                THUMBS_DIR / meta["content_hash"][:2] / f"{meta['content_hash']}.jpg"
            )
            final_thumb.parent.mkdir(parents=True, exist_ok=True)
            if thumb_dest.exists() and not final_thumb.exists():
                shutil.move(str(thumb_dest), str(final_thumb))
            meta["thumb_path"] = str(final_thumb)
    else:
        thumb_dest = THUMBS_DIR / (ch or "xx")[:2] / f"{ch or path.stem}.jpg"
        meta = analyze_image(dest, thumb_dest)
        final_thumb = (
            THUMBS_DIR / meta["content_hash"][:2] / f"{meta['content_hash']}.jpg"
        )
        if Path(meta["thumb_path"]) != final_thumb and Path(meta["thumb_path"]).exists():
            final_thumb.parent.mkdir(parents=True, exist_ok=True)
            if not final_thumb.exists():
                shutil.move(str(meta["thumb_path"]), str(final_thumb))
            meta["thumb_path"] = str(final_thumb)

    if not meta.get("exif_date") and created_at:
        try:
            dt = _parse_iso(created_at)
            if dt:
                meta["exif_date"] = dt.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
        except ValueError:
            pass

    rel = dest.relative_to(root)
    folder = str(rel.parent).replace("\\", "/")
    if folder == ".":
        folder = ""
    stat = dest.stat()
    rel_str = str(rel).replace("\\", "/")

    if meta["content_hash"] in known_hashes:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "action": "skip_exact",
            "match": known_hashes[meta["content_hash"]],
            "rel_path": known_hashes[meta["content_hash"]],
        }

    with connect() as conn:
        existing = conn.execute(
            "SELECT rel_path FROM photos WHERE content_hash = ?",
            (meta["content_hash"],),
        ).fetchone()
        if existing:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            known_hashes[meta["content_hash"]] = existing["rel_path"]
            return {
                "action": "skip_exact",
                "match": existing["rel_path"],
                "rel_path": existing["rel_path"],
            }
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
                rel_str,
                dest.name,
                folder,
                stat.st_size,
                stat.st_mtime,
                meta.get("width"),
                meta.get("height"),
                meta.get("exif_date"),
                meta.get("latitude"),
                meta.get("longitude"),
                meta.get("media_type") or "image",
                meta.get("duration"),
                meta["content_hash"],
                meta.get("phash"),
                meta.get("thumb_path"),
            ),
        )

    known_hashes[meta["content_hash"]] = rel_str
    if meta.get("phash"):
        library_phashes.append((meta["phash"], rel_str, meta["content_hash"]))

    return {"action": "copied", "rel_path": rel_str}


def thumb_path_for_asset(asset_id: str) -> Path:
    return ICLOUD_THUMBS_DIR / f"{asset_id}.jpg"
