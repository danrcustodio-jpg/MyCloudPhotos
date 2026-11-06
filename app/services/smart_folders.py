from __future__ import annotations

from datetime import datetime
from typing import Any

from app.database import connect, rows_to_dicts


def _this_year() -> str:
    return str(datetime.now().year)


def _last_year() -> str:
    return str(datetime.now().year - 1)


def _rules() -> list[dict[str, Any]]:
    """Smart-folder rule definitions (id, name, SQL WHERE without leading WHERE)."""
    y = _this_year()
    ly = _last_year()
    return [
        {
            "id": "screenshots",
            "name": "Screenshots",
            "where": (
                "(LOWER(filename) LIKE '%screenshot%' "
                "OR LOWER(filename) LIKE '%screen shot%' "
                "OR LOWER(filename) LIKE '%screen_shot%' "
                "OR (LOWER(filename) LIKE 'img_%' AND LOWER(filename) LIKE '%screen%'))"
            ),
            "params": [],
        },
        {
            "id": "root-dumps",
            "name": "Root dumps",
            "where": "folder = ''",
            "params": [],
        },
        {
            "id": "this-year",
            "name": "Added this year",
            "where": "strftime('%Y', datetime(mtime, 'unixepoch', 'localtime')) = ?",
            "params": [y],
        },
        {
            "id": "last-year",
            "name": "Added last year",
            "where": "strftime('%Y', datetime(mtime, 'unixepoch', 'localtime')) = ?",
            "params": [ly],
        },
        {
            "id": "videos",
            "name": "Videos",
            "where": "media_type = 'video'",
            "params": [],
        },
        {
            "id": "no-date",
            "name": "No date",
            "where": "exif_date IS NULL",
            "params": [],
        },
    ]


def _rule_by_id(folder_id: str) -> dict[str, Any] | None:
    for rule in _rules():
        if rule["id"] == folder_id:
            return rule
    return None


def list_smart_folders() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with connect() as conn:
        for rule in _rules():
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM photos WHERE {rule['where']}",
                rule["params"],
            ).fetchone()
            out.append({"id": rule["id"], "name": rule["name"], "count": int(row["c"])})
    try:
        from app.services import quarantine as quarantine_svc

        out.append(
            {
                "id": "quarantine",
                "name": "Quarantine",
                "count": quarantine_svc.quarantine_count(),
            }
        )
    except Exception:
        out.append({"id": "quarantine", "name": "Quarantine", "count": 0})
    return out


def list_smart_folder_photos(
    folder_id: str,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any] | None:
    if folder_id == "quarantine":
        from app.services import quarantine as quarantine_svc

        data = quarantine_svc.list_quarantine(limit=limit, offset=offset)
        return {
            "id": "quarantine",
            "name": "Quarantine",
            "total": data["total"],
            "limit": data["limit"],
            "offset": data["offset"],
            "items": data["items"],
            "quarantine_dir": data.get("quarantine_dir"),
        }
    rule = _rule_by_id(folder_id)
    if not rule:
        return None
    where = f"WHERE {rule['where']}"
    params = list(rule["params"])
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM photos {where}",
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT id, rel_path, filename, folder, size, width, height, exif_date,
                   content_hash, media_type, latitude, longitude, duration
            FROM photos
            {where}
            ORDER BY COALESCE(exif_date, '') DESC, filename ASC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {
        "id": rule["id"],
        "name": rule["name"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows_to_dicts(rows),
    }
