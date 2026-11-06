from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.config import PHASH_THRESHOLD, QUARANTINE_DIR_NAME, get_photo_root
from app.database import connect, rows_to_dicts, strip_photo_paths
from app.paths import assert_under_root
from app.services import quarantine as quarantine_svc

# Cache full near-dupe group list between paginated requests
_near_cache_key: tuple[Any, ...] | None = None
_near_cache_groups: list[dict[str, Any]] | None = None
_near_cache_compared: int = 0


def invalidate_near_cache() -> None:
    global _near_cache_key, _near_cache_groups, _near_cache_compared
    _near_cache_key = None
    _near_cache_groups = None
    _near_cache_compared = 0


def _phash_int(ph: str) -> int | None:
    try:
        return int(str(ph).strip(), 16)
    except (TypeError, ValueError):
        return None


def find_exact_duplicates(
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a page of exact duplicate groups (same content_hash)."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    with connect() as conn:
        total_groups = int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM (
                  SELECT content_hash
                  FROM photos
                  WHERE content_hash IS NOT NULL AND content_hash != ''
                  GROUP BY content_hash
                  HAVING COUNT(*) > 1
                )
                """
            ).fetchone()["n"]
        )
        hash_rows = conn.execute(
            """
            SELECT content_hash, COUNT(*) AS count
            FROM photos
            WHERE content_hash IS NOT NULL AND content_hash != ''
            GROUP BY content_hash
            HAVING count > 1
            ORDER BY count DESC, content_hash
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

        groups: list[dict[str, Any]] = []
        for hr in hash_rows:
            photos = conn.execute(
                """
                SELECT id, rel_path, filename, folder, size, width, height,
                       exif_date, content_hash, media_type, duration, path
                FROM photos WHERE content_hash = ?
                ORDER BY path
                """,
                (hr["content_hash"],),
            ).fetchall()
            items = rows_to_dicts(photos)
            items.sort(key=lambda p: (p.get("exif_date") or "", p["path"]), reverse=True)
            groups.append(
                {
                    "type": "exact",
                    "key": hr["content_hash"],
                    "count": hr["count"],
                    "photos": strip_photo_paths(items),
                }
            )

    return {
        "groups": groups,
        "total_groups": total_groups,
        "limit": limit,
        "offset": offset,
        "truncated": offset + len(groups) < total_groups,
    }


def phash_photo_count() -> int:
    with connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE phash IS NOT NULL AND phash != ''"
            ).fetchone()["c"]
        )


def find_near_duplicates(
    threshold: int | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Find near-duplicate clusters (perceptual hash Hamming distance).

    Exact same content_hash pairs are excluded (use Find exact for those).
    Uses vectorized NumPy comparison so large libraries finish in tens of seconds.
    """
    thr = PHASH_THRESHOLD if threshold is None else int(threshold)
    thr = max(0, min(thr, 32))
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    with connect() as conn:
        stamp = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(id), 0) AS m FROM photos "
            "WHERE phash IS NOT NULL AND phash != ''"
        ).fetchone()

    global _near_cache_key, _near_cache_groups, _near_cache_compared
    cache_key = (thr, int(stamp["c"]), int(stamp["m"]), "media_v1")
    if _near_cache_key == cache_key and _near_cache_groups is not None:
        all_groups = _near_cache_groups
        n = _near_cache_compared
    else:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, rel_path, filename, folder, size, width, height,
                       exif_date, content_hash, phash, media_type, duration, path
                FROM photos
                WHERE phash IS NOT NULL AND phash != ''
                ORDER BY id
                """
            ).fetchall()
        photos = rows_to_dicts(rows)
        for p in photos:
            p["_ph"] = _phash_int(p["phash"])
        photos = [p for p in photos if p["_ph"] is not None]

        by_hash: dict[str, list[dict]] = {}
        for p in photos:
            by_hash.setdefault(p["content_hash"] or f"id:{p['id']}", []).append(p)

        unique_reps: list[dict] = []
        seen_hash: set[str] = set()
        for p in photos:
            ch = p["content_hash"] or f"id:{p['id']}"
            if ch in seen_hash:
                continue
            seen_hash.add(ch)
            unique_reps.append(p)

        parent = {p["id"]: p["id"] for p in photos}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for members in by_hash.values():
            base = members[0]["id"]
            for m in members[1:]:
                union(base, m["id"])

        n = len(unique_reps)
        if n >= 2:
            arr = np.array([p["_ph"] for p in unique_reps], dtype=np.uint64)
            batch = 128
            for start in range(0, n, batch):
                end = min(n, start + batch)
                left = arr[start:end][:, None]
                right = arr[start:][None, :]
                dists = np.bitwise_count(np.bitwise_xor(left, right))
                for i in range(end - start):
                    dists[i, : i + 1] = 255
                for i in range(end - start):
                    hits = np.flatnonzero(dists[i] <= thr)
                    if hits.size > 500:
                        hits = hits[:500]
                    ai = start + i
                    a_id = unique_reps[ai]["id"]
                    for j in hits.tolist():
                        union(a_id, unique_reps[start + j]["id"])

        clusters: dict[int, list[dict]] = {}
        for p in photos:
            clusters.setdefault(find(p["id"]), []).append(p)

        all_groups = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            hashes = {m["content_hash"] for m in members}
            if len(hashes) == 1:
                continue
            members.sort(key=lambda p: (p.get("exif_date") or "", p["path"]), reverse=True)
            clean = [{k: v for k, v in m.items() if k != "_ph"} for m in members]
            shown = clean[:24]
            all_groups.append(
                {
                    "type": "near",
                    "key": f"near-{clean[0]['id']}",
                    "count": len(clean),
                    "photos": strip_photo_paths(shown),
                    "photos_truncated": len(clean) > len(shown),
                }
            )

        all_groups.sort(key=lambda g: g["count"], reverse=True)
        _near_cache_key = cache_key
        _near_cache_groups = all_groups
        _near_cache_compared = n

    total_groups = len(all_groups)
    page = all_groups[offset : offset + limit]

    return {
        "groups": page,
        "total_groups": total_groups,
        "limit": limit,
        "offset": offset,
        "truncated": offset + len(page) < total_groups,
        "compared": n,
        "threshold": thr,
    }


def resolve_duplicates(
    keep_id: int | None,
    delete_ids: list[int],
    mode: str = "quarantine",
    *,
    delete_all: bool = False,
) -> dict[str, Any]:
    """
    Keep one photo and quarantine/delete the others, or remove every id in
    delete_ids when delete_all=True (no keep).
    """
    if mode not in ("quarantine", "delete"):
        raise ValueError("mode must be 'quarantine' or 'delete'")
    if not delete_ids and not delete_all:
        raise ValueError("No photos to remove")

    root = get_photo_root()
    quarantine_dir = str(root / QUARANTINE_DIR_NAME) if mode == "quarantine" else None
    results: dict[str, Any] = {
        "kept": None if delete_all else keep_id,
        "removed": [],
        "errors": [],
        "mode": mode,
        "quarantine_dir": quarantine_dir,
        "delete_all": bool(delete_all),
    }

    with connect() as conn:
        if not delete_all:
            if keep_id is None:
                raise ValueError("keep_id is required unless delete_all is true")
            keep = conn.execute("SELECT * FROM photos WHERE id = ?", (keep_id,)).fetchone()
            if not keep:
                raise ValueError(f"Keep photo not found: {keep_id}")
            targets = [pid for pid in delete_ids if pid != keep_id]
        else:
            targets = list(dict.fromkeys(delete_ids))

        for pid in targets:
            row = conn.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()
            if not row:
                results["errors"].append({"id": pid, "error": "not found"})
                continue
            try:
                src = assert_under_root(Path(row["path"]), root)
            except ValueError as e:
                results["errors"].append({"id": pid, "error": str(e)})
                continue
            try:
                if not src.exists():
                    # File already gone on disk — still drop from index
                    conn.execute("DELETE FROM photos WHERE id = ?", (pid,))
                    results["removed"].append(pid)
                    continue
                if mode == "quarantine":
                    dest_dir = quarantine_svc.ensure_quarantine_dir()
                    dest = dest_dir / src.name
                    if dest.exists():
                        stem, suffix = dest.stem, dest.suffix
                        n = 1
                        while dest.exists():
                            dest = dest_dir / f"{stem}_{n}{suffix}"
                            n += 1
                    shutil.move(str(src), str(dest))
                    conn.execute(
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
                            str(dest),
                            row["path"],
                            row["rel_path"],
                            row["filename"] or dest.name,
                            row["size"],
                            row["content_hash"],
                            row["thumb_path"],
                        ),
                    )
                else:
                    src.unlink(missing_ok=True)
                    thumb = row["thumb_path"]
                    if thumb:
                        Path(thumb).unlink(missing_ok=True)

                conn.execute("DELETE FROM photos WHERE id = ?", (pid,))
                results["removed"].append(pid)
            except OSError as e:
                results["errors"].append({"id": pid, "error": str(e)})

    if results["removed"]:
        invalidate_near_cache()

    results["removed_count"] = len(results["removed"])
    results["error_count"] = len(results["errors"])
    results["ok"] = bool(results["removed"]) and not results["errors"]
    return results


def _keep_sort_key(row: dict[str, Any] | Any) -> tuple:
    """Prefer dated photos, then stable path order (same as UI default keep)."""
    if hasattr(row, "keys"):
        exif = row["exif_date"] if "exif_date" in row.keys() else None
        path = row["path"] if "path" in row.keys() else ""
    else:
        exif = row.get("exif_date")
        path = row.get("path") or ""
    return (exif or "", path or "")


def preview_exact_dedupe() -> dict[str, Any]:
    """Count exact-dupe groups and how many files would be removed (keeping one each)."""
    with connect() as conn:
        hash_rows = conn.execute(
            """
            SELECT content_hash, COUNT(*) AS count,
                   COALESCE(SUM(size), 0) AS bytes
            FROM photos
            WHERE content_hash IS NOT NULL AND content_hash != ''
            GROUP BY content_hash
            HAVING count > 1
            """
        ).fetchall()
    groups = len(hash_rows)
    extras = 0
    bytes_extra = 0
    for hr in hash_rows:
        c = int(hr["count"])
        extras += c - 1
        # Approximate removable bytes as (count-1)/count of group size
        bytes_extra += int(hr["bytes"] * (c - 1) / c) if c else 0
    return {
        "groups": groups,
        "files_to_keep": groups,
        "files_to_remove": extras,
        "approx_bytes_to_remove": bytes_extra,
        "mode_default": "delete",
    }


@dataclass
class ExactDedupeBatchState:
    status: str = "idle"  # idle | running | completed | cancelled | error
    mode: str = "delete"
    groups_total: int = 0
    groups_done: int = 0
    files_removed: int = 0
    files_kept: int = 0
    errors: int = 0
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


_batch = ExactDedupeBatchState()


def get_exact_dedupe_batch_status() -> dict[str, Any]:
    with _batch.lock:
        elapsed = None
        if _batch.started_at is not None:
            end = _batch.finished_at or time.time()
            elapsed = round(end - _batch.started_at, 1)
        pct = None
        if _batch.groups_total > 0:
            pct = min(100, round(100 * _batch.groups_done / _batch.groups_total))
        return {
            "status": _batch.status,
            "mode": _batch.mode,
            "groups_total": _batch.groups_total,
            "groups_done": _batch.groups_done,
            "files_removed": _batch.files_removed,
            "files_kept": _batch.files_kept,
            "errors": _batch.errors,
            "message": _batch.message,
            "elapsed_seconds": elapsed,
            "percent": pct,
            "cancel_requested": _batch.cancel_requested,
        }


def request_exact_dedupe_batch_cancel() -> dict[str, Any]:
    with _batch.lock:
        if _batch.status == "running":
            _batch.cancel_requested = True
            _batch.message = "Cancel requested — finishing current group…"
    return get_exact_dedupe_batch_status()


def start_exact_dedupe_batch(*, mode: str = "delete", confirm: bool = False) -> dict[str, Any]:
    """
    Process every exact-duplicate group: keep one file, remove the rest.
    mode must be 'delete' (permanent) or 'quarantine'.
    confirm=True is required.
    """
    if mode not in ("delete", "quarantine"):
        raise ValueError("mode must be 'delete' or 'quarantine'")
    if not confirm:
        raise ValueError("confirm=true is required to start a full exact dedupe batch")

    from app.services.jobs import JOB_DEDUPE_BATCH, assert_can_start, release_job

    assert_can_start(JOB_DEDUPE_BATCH)

    try:
        preview = preview_exact_dedupe()
        if preview["groups"] == 0:
            raise ValueError("No exact duplicate groups found")
    except Exception:
        release_job(JOB_DEDUPE_BATCH)
        raise

    with _batch.lock:
        if _batch.status == "running":
            raise RuntimeError("An exact dedupe batch is already running")
        _batch.status = "running"
        _batch.mode = mode
        _batch.groups_total = preview["groups"]
        _batch.groups_done = 0
        _batch.files_removed = 0
        _batch.files_kept = 0
        _batch.errors = 0
        _batch.cancel_requested = False
        _batch.started_at = time.time()
        _batch.finished_at = None
        _batch.message = (
            f"Starting full exact dedupe ({mode}): "
            f"{preview['groups']:,} groups, ~{preview['files_to_remove']:,} files to remove…"
        )

    thread = threading.Thread(target=_run_exact_dedupe_batch, args=(mode,), daemon=True)
    thread.start()
    return get_exact_dedupe_batch_status()


def _run_exact_dedupe_batch(mode: str) -> None:
    from app.services.jobs import JOB_DEDUPE_BATCH, release_job

    try:
        get_photo_root()  # fail fast if not configured
        with connect() as conn:
            hash_rows = conn.execute(
                """
                SELECT content_hash, COUNT(*) AS count
                FROM photos
                WHERE content_hash IS NOT NULL AND content_hash != ''
                GROUP BY content_hash
                HAVING count > 1
                ORDER BY count DESC, content_hash
                """
            ).fetchall()

        with _batch.lock:
            _batch.groups_total = len(hash_rows)

        for i, hr in enumerate(hash_rows):
            with _batch.lock:
                if _batch.cancel_requested:
                    _batch.status = "cancelled"
                    _batch.finished_at = time.time()
                    _batch.message = (
                        f"Cancelled after {_batch.groups_done:,} / {_batch.groups_total:,} groups; "
                        f"removed {_batch.files_removed:,} files"
                    )
                    invalidate_near_cache()
                    release_job(JOB_DEDUPE_BATCH)
                    return

            with connect() as conn:
                photos = conn.execute(
                    """
                    SELECT id, path, exif_date, size
                    FROM photos WHERE content_hash = ?
                    """,
                    (hr["content_hash"],),
                ).fetchall()
            items = [dict(p) for p in photos]
            if len(items) < 2:
                with _batch.lock:
                    _batch.groups_done += 1
                continue

            items.sort(key=_keep_sort_key, reverse=True)
            keep_id = int(items[0]["id"])
            delete_ids = [int(p["id"]) for p in items[1:]]

            try:
                result = resolve_duplicates(keep_id, delete_ids, mode=mode, delete_all=False)
                removed_n = int(result.get("removed_count") or 0)
                err_n = int(result.get("error_count") or 0)
            except Exception as e:
                removed_n = 0
                err_n = 1
                with _batch.lock:
                    _batch.message = f"Error on group {i + 1}: {e}"

            with _batch.lock:
                _batch.groups_done += 1
                _batch.files_kept += 1
                _batch.files_removed += removed_n
                _batch.errors += err_n
                if _batch.groups_done % 10 == 0 or _batch.groups_done == _batch.groups_total:
                    _batch.message = (
                        f"Exact dedupe ({mode}): {_batch.groups_done:,}/{_batch.groups_total:,} groups · "
                        f"removed {_batch.files_removed:,} · errors {_batch.errors:,}"
                    )

        with _batch.lock:
            _batch.status = "completed"
            _batch.finished_at = time.time()
            _batch.message = (
                f"Complete: kept {_batch.files_kept:,}, permanently removed {_batch.files_removed:,} "
                f"across {_batch.groups_done:,} groups"
                if mode == "delete"
                else (
                    f"Complete: kept {_batch.files_kept:,}, quarantined {_batch.files_removed:,} "
                    f"across {_batch.groups_done:,} groups"
                )
            )
            if _batch.errors:
                _batch.message += f" · {_batch.errors:,} errors"
        invalidate_near_cache()
        release_job(JOB_DEDUPE_BATCH)
    except Exception as e:
        with _batch.lock:
            _batch.status = "error"
            _batch.finished_at = time.time()
            _batch.message = f"Batch failed: {e}"
        invalidate_near_cache()
        release_job(JOB_DEDUPE_BATCH)
