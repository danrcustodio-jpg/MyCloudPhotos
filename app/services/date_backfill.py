"""Infer missing photo dates from folder paths / filenames."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Any

from app.database import connect

# Prefer more specific matches first.
_YMD = re.compile(
    r"(?:^|[/\\])((?:19|20)\d{2})[-_/](0[1-9]|1[0-2])[-_/](0[1-9]|[12]\d|3[01])(?:[/\\]|$)"
)
_YM = re.compile(
    r"(?:^|[/\\])((?:19|20)\d{2})[-_/](0[1-9]|1[0-2])(?:[/\\]|$)"
)
# Bare year segment: 2026 or 2026/something (not consumed as YM already)
_YEAR = re.compile(r"(?:^|[/\\])((?:19|20)\d{2})(?:[/\\]|$)")
# Screenshot_20221112-101659 / IMG_20240115_120000
_FILENAME_YMD = re.compile(
    r"(?:^|[^0-9])((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?:[^0-9]|$)"
)


def _valid_ymd(y: int, m: int, d: int) -> bool:
    if y < 1990 or y > 2100 or m < 1 or m > 12 or d < 1:
        return False
    return d <= monthrange(y, m)[1]


def _fmt(y: int, m: int, d: int) -> str:
    return date(y, m, d).isoformat() + " 12:00:00"


def infer_date_from_paths(
    *,
    folder: str | None = None,
    rel_path: str | None = None,
    filename: str | None = None,
) -> dict[str, str] | None:
    """
    Return {exif_date, source} or None.
    source: folder_day | folder_month | folder_year | filename_day
    """
    path_text = "/".join(
        p.replace("\\", "/").strip("/")
        for p in (folder or "", rel_path or "")
        if p
    )
    if path_text:
        m = _YMD.search("/" + path_text + "/")
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if _valid_ymd(y, mo, d):
                return {"exif_date": _fmt(y, mo, d), "source": "folder_day"}
        m = _YM.search("/" + path_text + "/")
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            if _valid_ymd(y, mo, 1):
                return {"exif_date": _fmt(y, mo, 1), "source": "folder_month"}
        m = _YEAR.search("/" + path_text + "/")
        if m:
            y = int(m.group(1))
            if _valid_ymd(y, 1, 1):
                return {"exif_date": _fmt(y, 1, 1), "source": "folder_year"}

    name = filename or ""
    if not name and rel_path:
        name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    if name:
        m = _FILENAME_YMD.search(name)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if _valid_ymd(y, mo, d):
                return {"exif_date": _fmt(y, mo, d), "source": "filename_day"}
    return None


def preview_date_backfill(*, sample_limit: int = 25) -> dict[str, Any]:
    """Count undated photos that can get a date from path/filename."""
    by_source: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    would_update = 0
    undated = 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, folder, rel_path, filename
            FROM photos
            WHERE exif_date IS NULL OR trim(exif_date) = ''
            """
        ).fetchall()
        undated = len(rows)
        for row in rows:
            inferred = infer_date_from_paths(
                folder=row["folder"],
                rel_path=row["rel_path"],
                filename=row["filename"],
            )
            if not inferred:
                continue
            would_update += 1
            src = inferred["source"]
            by_source[src] = by_source.get(src, 0) + 1
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "id": row["id"],
                        "filename": row["filename"],
                        "folder": row["folder"],
                        "inferred_date": inferred["exif_date"],
                        "source": src,
                    }
                )
    return {
        "undated": undated,
        "would_update": would_update,
        "unmatched": undated - would_update,
        "by_source": by_source,
        "samples": samples,
    }


def apply_date_backfill() -> dict[str, Any]:
    """Write inferred dates into photos.exif_date where currently empty."""
    updated = 0
    by_source: dict[str, int] = {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, folder, rel_path, filename
            FROM photos
            WHERE exif_date IS NULL OR trim(exif_date) = ''
            """
        ).fetchall()
        for row in rows:
            inferred = infer_date_from_paths(
                folder=row["folder"],
                rel_path=row["rel_path"],
                filename=row["filename"],
            )
            if not inferred:
                continue
            cur = conn.execute(
                "UPDATE photos SET exif_date = ? WHERE id = ? "
                "AND (exif_date IS NULL OR trim(exif_date) = '')",
                (inferred["exif_date"], row["id"]),
            )
            if cur.rowcount:
                updated += 1
                src = inferred["source"]
                by_source[src] = by_source.get(src, 0) + 1
    return {
        "updated": updated,
        "by_source": by_source,
        "message": f"Filled dates on {updated:,} photos from folder/filename.",
    }
