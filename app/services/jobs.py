"""Coordinate heavy DB-writing jobs so only one runs at a time."""

from __future__ import annotations

import threading
from typing import Any

# Job ids used by API + UI
JOB_SCAN = "scan"
JOB_IMPORT = "import"
JOB_INVENTORY = "inventory"
JOB_ARCHIVE = "archive"
JOB_DEDUPE_BATCH = "dedupe_batch"
JOB_CLEANUP = "cleanup"
JOB_ORGANIZE = "organize"
JOB_RAW_CONVERT = "raw_convert"
JOB_FACE_SCAN = "face_scan"

HEAVY_JOBS = (
    JOB_SCAN,
    JOB_IMPORT,
    JOB_INVENTORY,
    JOB_ARCHIVE,
    JOB_DEDUPE_BATCH,
    JOB_CLEANUP,
    JOB_ORGANIZE,
    JOB_RAW_CONVERT,
    JOB_FACE_SCAN,
)

# Actions that are safe while a heavy job runs (for UI documentation)
SAFE_WHILE_BUSY = [
    "Browse / search / lightbox",
    "Map",
    "Archive estimate + load grid (read-only)",
    "Duplicates preview (Find exact / near)",
    "Import preview",
    "RAW convert preview",
    "Settings view",
    "Cancel the active job",
]

BLOCKED_WHILE_BUSY = [
    "Library scan / full rehash",
    "Import start",
    "iCloud inventory refresh",
    "iCloud archive start",
    "Full exact dedupe batch",
    "Junk / corrupt file cleanup",
    "Organize by date / bulk move",
    "RAW to JPEG convert",
    "Face scan",
    "Switching family iCloud account while inventory/archive runs",
]

_PHASE_LABELS = {
    "idle": "Idle",
    "scanning": "Scanning",
    "finishing": "Finishing",
    "paused": "Paused",
    "cancelling": "Cancelling",
    "starting": "Starting",
    "download": "Downloading",
    "import": "Importing",
    "verify": "Verifying",
    "remove_from_icloud": "Removing from iCloud",
    "indexing": "Indexing iCloud",
    "moving": "Moving files",
    "cleanup": "Cleaning folders",
    "converting": "Converting",
    "deleting": "Deleting",
    "clustering": "Clustering faces",
}

# Hard start mutex — claim before flipping any job to "running"
_claim_lock = threading.Lock()
_claimed_job_id: str | None = None


def _pct(processed: int | None, total: int | None) -> int | None:
    if processed is None or not total or total <= 0:
        return None
    return min(100, round(100 * int(processed) / int(total)))


def _phase_label(phase: str | None) -> str:
    if not phase:
        return ""
    key = str(phase).strip().lower()
    if key in _PHASE_LABELS:
        return _PHASE_LABELS[key]
    return str(phase).replace("_", " ").strip().title()


def _format_eta(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        return None
    if sec < 0 or sec != sec:  # NaN
        return None
    if sec < 60:
        return f"~{max(1, int(round(sec)))}s left"
    if sec < 3600:
        mins = int(round(sec / 60))
        return f"~{mins} min left"
    hours = int(sec // 3600)
    mins = int(round((sec % 3600) / 60))
    if mins:
        return f"~{hours}h {mins}m left"
    return f"~{hours}h left"


def _job(
    *,
    job_id: str,
    label: str,
    detail: str,
    cancel: str,
    processed: int | None = None,
    total: int | None = None,
    percent: int | None = None,
    indeterminate: bool = False,
    phase: str | None = None,
    phase_label: str | None = None,
    unit: str = "items",
    eta_seconds: float | int | None = None,
    eta_label: str | None = None,
    rate_label: str | None = None,
    stats: str | None = None,
    current: str | None = None,
    cancelling: bool = False,
    paused: bool = False,
    pause: str | None = None,
    resume: str | None = None,
) -> dict[str, Any]:
    pct = percent if percent is not None else _pct(processed, total)
    eta = eta_label or _format_eta(eta_seconds)
    return {
        "id": job_id,
        "label": label,
        "status": "running",
        "phase": phase or "",
        "phase_label": phase_label if phase_label is not None else _phase_label(phase),
        "detail": detail,
        "cancel": cancel,
        "processed": processed,
        "total": total,
        "percent": pct,
        "unit": unit,
        "eta_seconds": round(float(eta_seconds), 1) if eta_seconds is not None else None,
        "eta_label": eta,
        "rate_label": rate_label or "",
        "stats": stats or "",
        "current": current or "",
        "indeterminate": bool(indeterminate) or (pct is None and not total),
        "cancelling": bool(cancelling),
        "paused": bool(paused),
        "pause": pause,
        "resume": resume,
    }


def get_busy_status() -> dict[str, Any]:
    """Return which heavy jobs are running (at most one expected)."""
    from app.services import dedupe, icloud_archive, importer, scanner

    jobs: list[dict[str, Any]] = []

    scan = scanner.get_scan_status()
    if scan.get("status") == "running":
        total = int(scan.get("total") or 0)
        processed = int(scan.get("processed") or 0)
        paused = bool(scan.get("paused"))
        rate = scan.get("rate_per_sec")
        rate_label = f"{rate:.1f}/s" if isinstance(rate, (int, float)) and rate > 0 else None
        bytes_rate = scan.get("rate_bytes_per_sec")
        if isinstance(bytes_rate, (int, float)) and bytes_rate > 0:
            mb = bytes_rate / (1024 * 1024)
            rate_label = (f"{rate_label} · {mb:.1f} MB/s" if rate_label else f"{mb:.1f} MB/s")
        stats_bits = []
        if scan.get("added"):
            stats_bits.append(f"added {int(scan['added']):,}")
        if scan.get("updated"):
            stats_bits.append(f"updated {int(scan['updated']):,}")
        if scan.get("errors"):
            stats_bits.append(f"errors {int(scan['errors']):,}")
        jobs.append(
            _job(
                job_id=JOB_SCAN,
                label="Library scan",
                detail=scan.get("message") or scan.get("phase") or "",
                cancel="/api/scan/cancel",
                processed=processed,
                total=total if total > 0 else None,
                indeterminate=total <= 0,
                paused=paused,
                phase="paused" if paused else (scan.get("phase") or ""),
                unit="files",
                eta_seconds=scan.get("eta_seconds"),
                eta_label=scan.get("eta_label"),
                rate_label=rate_label,
                stats=" · ".join(stats_bits),
                pause=None if paused else "/api/scan/pause",
                resume="/api/scan/resume" if paused else None,
            )
        )

    imp = importer.get_import_status()
    if imp.get("status") == "running":
        stats_bits = []
        if imp.get("copied"):
            stats_bits.append(f"copied {int(imp['copied']):,}")
        if imp.get("skipped_exact"):
            stats_bits.append(f"exact skip {int(imp['skipped_exact']):,}")
        if imp.get("skipped_near"):
            stats_bits.append(f"near skip {int(imp['skipped_near']):,}")
        if imp.get("errors"):
            stats_bits.append(f"errors {int(imp['errors']):,}")
        paused = bool(imp.get("paused"))
        jobs.append(
            _job(
                job_id=JOB_IMPORT,
                label="Import",
                detail=imp.get("message") or "",
                cancel="/api/import/cancel",
                processed=int(imp.get("processed") or 0),
                total=int(imp.get("total") or 0) or None,
                unit="files",
                stats=" · ".join(stats_bits),
                paused=paused,
                phase="paused" if paused else "",
                pause=None if paused else "/api/import/pause",
                resume="/api/import/resume" if paused else None,
            )
        )

    inv = icloud_archive.get_inventory_status(lightweight=True)
    if inv.get("status") == "running":
        total = int(inv.get("total") or 0)
        cancelling = bool(inv.get("cancel_requested"))
        jobs.append(
            _job(
                job_id=JOB_INVENTORY,
                label="iCloud inventory",
                detail="Cancelling…" if cancelling else (inv.get("message") or ""),
                cancel="/api/icloud/inventory/cancel",
                processed=int(inv.get("processed") or 0),
                total=total if total > 0 else None,
                indeterminate=total <= 0,
                phase="cancelling" if cancelling else "indexing",
                cancelling=cancelling,
                unit="assets",
            )
        )

    arch = icloud_archive.get_archive_status()
    if arch.get("status") == "running":
        stats_bits = []
        if arch.get("downloaded"):
            stats_bits.append(f"downloaded {int(arch['downloaded']):,}")
        if arch.get("imported"):
            stats_bits.append(f"imported {int(arch['imported']):,}")
        if arch.get("verified"):
            stats_bits.append(f"verified {int(arch['verified']):,}")
        if arch.get("deleted_icloud"):
            stats_bits.append(f"removed {int(arch['deleted_icloud']):,}")
        if arch.get("errors"):
            stats_bits.append(f"errors {int(arch['errors']):,}")
        batch_i = arch.get("batch_index") or 0
        batch_n = arch.get("batch_count") or 0
        if batch_n:
            stats_bits.insert(0, f"batch {batch_i}/{batch_n}")
        paused = bool(arch.get("paused"))
        cancelling = bool(arch.get("cancel_requested"))
        jobs.append(
            _job(
                job_id=JOB_ARCHIVE,
                label="iCloud archive",
                detail="Cancelling…" if cancelling else (arch.get("message") or arch.get("phase") or ""),
                cancel="/api/icloud/archive/cancel",
                processed=int(arch.get("processed") or 0),
                total=int(arch.get("total") or 0) or None,
                phase="cancelling" if cancelling else (
                    "paused" if paused else (arch.get("phase") or "")
                ),
                unit="assets",
                stats=" · ".join(stats_bits),
                cancelling=cancelling,
                paused=paused and not cancelling,
                pause=None if (paused or cancelling) else "/api/icloud/archive/pause",
                resume="/api/icloud/archive/resume" if paused else None,
            )
        )

    batch = dedupe.get_exact_dedupe_batch_status()
    if batch.get("status") == "running":
        jobs.append(
            _job(
                job_id=JOB_DEDUPE_BATCH,
                label="Exact dedupe batch",
                detail=batch.get("message") or "",
                cancel="/api/duplicates/exact/batch/cancel",
                processed=int(batch.get("groups_done") or 0),
                total=int(batch.get("groups_total") or 0) or None,
                percent=batch.get("percent"),
                unit="groups",
            )
        )

    from app.services import cleanup, faces, organize, raw_convert

    clean = cleanup.get_cleanup_status()
    if clean.get("status") == "running":
        phase = clean.get("phase") or ""
        if phase == "deleting" and clean.get("total"):
            jobs.append(
                _job(
                    job_id=JOB_CLEANUP,
                    label="Junk cleanup",
                    detail=clean.get("message") or "",
                    cancel="/api/cleanup/cancel",
                    processed=int(clean.get("processed") or 0),
                    total=int(clean.get("total") or 0),
                    phase=phase,
                    unit="files",
                    stats=(
                        f"freed {_fmt_bytes(clean.get('bytes_freed'))}"
                        if clean.get("bytes_freed")
                        else ""
                    ),
                )
            )
        else:
            jobs.append(
                _job(
                    job_id=JOB_CLEANUP,
                    label="Junk cleanup",
                    detail=clean.get("message") or "",
                    cancel="/api/cleanup/cancel",
                    processed=int(clean.get("scanned") or 0),
                    total=None,
                    indeterminate=True,
                    phase=phase or "scanning",
                    unit="files",
                )
            )

    org = organize.get_organize_status()
    if org.get("status") == "running":
        cancelling = bool(org.get("cancel_requested")) or org.get("phase") == "cancelling"
        paused = bool(org.get("paused")) and not cancelling
        stats_bits = []
        if org.get("moved"):
            stats_bits.append(f"moved {int(org['moved']):,}")
        if org.get("skipped"):
            stats_bits.append(f"skipped {int(org['skipped']):,}")
        if org.get("errors"):
            stats_bits.append(f"errors {int(org['errors']):,}")
        jobs.append(
            _job(
                job_id=JOB_ORGANIZE,
                label="Organize by date",
                detail="Cancelling…" if cancelling else (org.get("message") or ""),
                cancel="/api/organize/cancel",
                processed=int(org.get("processed") or 0),
                total=int(org.get("total") or 0) or None,
                phase=org.get("phase") or "",
                cancelling=cancelling,
                paused=paused,
                unit="photos",
                stats=" · ".join(stats_bits),
                pause=None if (paused or cancelling) else "/api/organize/pause",
                resume="/api/organize/resume" if paused else None,
            )
        )

    raw = raw_convert.get_raw_convert_status()
    if raw.get("status") == "running":
        cancelling = bool(raw.get("cancel_requested")) or raw.get("phase") == "cancelling"
        paused = bool(raw.get("paused")) and not cancelling
        stats_bits = []
        if raw.get("converted"):
            stats_bits.append(f"converted {int(raw['converted']):,}")
        if raw.get("quarantined"):
            stats_bits.append(f"quarantined {int(raw['quarantined']):,}")
        if raw.get("skipped"):
            stats_bits.append(f"skipped {int(raw['skipped']):,}")
        if raw.get("errors"):
            stats_bits.append(f"errors {int(raw['errors']):,}")
        jobs.append(
            _job(
                job_id=JOB_RAW_CONVERT,
                label="RAW to JPEG",
                detail="Cancelling…" if cancelling else (raw.get("message") or ""),
                cancel="/api/raw/cancel",
                processed=int(raw.get("processed") or 0),
                total=int(raw.get("total") or 0) or None,
                phase=raw.get("phase") or "",
                cancelling=cancelling,
                paused=paused,
                unit="files",
                stats=" · ".join(stats_bits),
                pause=None if (paused or cancelling) else "/api/raw/pause",
                resume="/api/raw/resume" if paused else None,
            )
        )

    face = faces.get_face_scan_status()
    if face.get("status") == "running":
        cancelling = bool(face.get("cancel_requested")) or face.get("phase") == "cancelling"
        paused = bool(face.get("paused")) and not cancelling
        rate = face.get("images_per_sec")
        rate_label = f"{rate:.1f} img/s" if isinstance(rate, (int, float)) and rate > 0 else None
        stats_bits = []
        if face.get("faces_found"):
            stats_bits.append(f"faces {int(face['faces_found']):,}")
        if face.get("auto_assigned"):
            stats_bits.append(f"auto-named {int(face['auto_assigned']):,}")
        if face.get("errors"):
            stats_bits.append(f"errors {int(face['errors']):,}")
        current = ""
        cur = face.get("current_photo") or {}
        if isinstance(cur, dict):
            current = cur.get("filename") or cur.get("rel_path") or ""
        jobs.append(
            _job(
                job_id=JOB_FACE_SCAN,
                label="Face scan",
                detail="Cancelling…" if cancelling else (face.get("message") or ""),
                cancel="/api/faces/scan/cancel",
                processed=int(face.get("processed") or 0),
                total=int(face.get("total") or 0) or None,
                phase=face.get("phase") or "",
                cancelling=cancelling,
                paused=paused,
                unit="images",
                eta_seconds=face.get("eta_sec"),
                rate_label=rate_label,
                stats=" · ".join(stats_bits),
                current=str(current) if current else None,
                pause=None if (paused or cancelling) else "/api/faces/scan/pause",
                resume="/api/faces/scan/resume" if paused else None,
            )
        )

    active = jobs[0] if jobs else None
    return {
        "busy": len(jobs) > 0 or _claimed_job_id is not None,
        "active": active,
        "claimed_job_id": _claimed_job_id,
        "jobs": jobs,
        "safe_while_busy": SAFE_WHILE_BUSY,
        "blocked_while_busy": BLOCKED_WHILE_BUSY,
    }


def _fmt_bytes(n: Any) -> str:
    try:
        b = int(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b / 1024:.1f} KB"
    if b < 1024**3:
        return f"{b / 1024**2:.1f} MB"
    return f"{b / 1024**3:.2f} GB"


def assert_can_start(job_id: str) -> None:
    """
    Atomically claim a heavy job slot.
    Call release_job(job_id) when the job finishes (any terminal state).
    """
    global _claimed_job_id
    with _claim_lock:
        status = get_busy_status()
        others = [j for j in status["jobs"] if j["id"] != job_id]
        if _claimed_job_id and _claimed_job_id != job_id:
            raise RuntimeError(
                f"Another job is starting or running ({_claimed_job_id}). "
                "Only one database-heavy job can run at a time — "
                "wait for it to finish or cancel it first."
            )
        if others:
            names = ", ".join(j["label"] for j in others)
            raise RuntimeError(
                f"Another job is running ({names}). "
                "Only one database-heavy job can run at a time — "
                "wait for it to finish or cancel it first. "
                "Browse, Map, and Archive estimates still work meanwhile."
            )
        _claimed_job_id = job_id


def release_job(job_id: str) -> None:
    """Release the hard claim when a job reaches a terminal state."""
    global _claimed_job_id
    with _claim_lock:
        if _claimed_job_id == job_id:
            _claimed_job_id = None


def assert_can_switch_icloud_account() -> None:
    """Block family-account switches while inventory or archive is running."""
    from app.services import icloud_archive

    inv = icloud_archive.get_inventory_status(lightweight=True)
    if inv.get("status") == "running":
        raise RuntimeError(
            "iCloud inventory is running — wait or cancel before switching accounts."
        )
    arch = icloud_archive.get_archive_status()
    if arch.get("status") == "running":
        raise RuntimeError(
            "iCloud archive is running — wait or cancel before switching accounts."
        )
