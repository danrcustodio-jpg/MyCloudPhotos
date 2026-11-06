from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import __version__
import app.config as config
from app.config import (
    PHASH_THRESHOLD,
    PHOTO_ROOT,
    ensure_data_dirs,
    get_photo_root,
    get_scan_workers,
    photo_root_configured,
    set_photo_root,
    set_scan_workers,
)
from app.database import init_db
from app.paths import assert_under_root
from app.services import (
    cleanup,
    date_backfill,
    dedupe,
    faces,
    icloud_archive,
    icloud_client,
    importer,
    jobs,
    organize,
    quarantine,
    raw_convert,
    scanner,
    smart_folders,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    init_db()
    scanner.recover_orphaned_jobs()
    yield


app = FastAPI(title="My Cloud Photos", version=__version__, lifespan=lifespan)

# Ensure DB exists even if lifespan hasn't run yet (e.g. imports / tooling)
ensure_data_dirs()
init_db()

# ----- Settings -----


class SettingsBody(BaseModel):
    photo_root: str | None = None
    scan_workers: int | None = Field(default=None, ge=1, le=8)


@app.get("/api/settings")
def api_settings():
    configured = photo_root_configured()
    root = None
    if configured:
        try:
            root = str(get_photo_root())
        except ValueError:
            configured = False
    return {
        "photo_root": root or str(PHOTO_ROOT) if PHOTO_ROOT.parts else None,
        "configured": configured,
        "scan_workers": get_scan_workers(),
        "phash_threshold": PHASH_THRESHOLD,
        "version": __version__,
    }


@app.post("/api/settings")
def api_set_settings(body: SettingsBody):
    if body.photo_root is None and body.scan_workers is None:
        raise HTTPException(400, "Provide photo_root and/or scan_workers")
    result: dict = {"configured": photo_root_configured()}
    if body.photo_root is not None:
        try:
            path = set_photo_root(body.photo_root)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        result["photo_root"] = str(path)
        result["configured"] = True
    else:
        try:
            result["photo_root"] = str(get_photo_root()) if photo_root_configured() else None
        except ValueError:
            result["photo_root"] = None
    if body.scan_workers is not None:
        try:
            result["scan_workers"] = set_scan_workers(body.scan_workers)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    else:
        result["scan_workers"] = get_scan_workers()
    return result


@app.get("/api/jobs/busy")
def api_jobs_busy():
    """Which heavy DB jobs are running — UI uses this to disable conflicting actions."""
    return jobs.get_busy_status()


# ----- Date backfill (folder / filename → missing exif_date) -----


@app.get("/api/dates/backfill/preview")
def api_dates_backfill_preview():
    return date_backfill.preview_date_backfill()


@app.post("/api/dates/backfill")
def api_dates_backfill():
    return date_backfill.apply_date_backfill()


# ----- Cleanup (junk / corrupt) -----


class CleanupPreviewBody(BaseModel):
    check_corrupt: bool = True


class CleanupStartBody(BaseModel):
    confirm: bool = False
    confirm_token: str | None = None
    delete_junk: bool = True
    delete_corrupt: bool = True
    check_corrupt: bool = True


@app.post("/api/cleanup/preview")
def api_cleanup_preview(body: CleanupPreviewBody | None = None):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        check = True if body is None else body.check_corrupt
        return cleanup.preview_cleanup(check_corrupt=check)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/cleanup/start")
def api_cleanup_start(body: CleanupStartBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return cleanup.start_cleanup(
            confirm=body.confirm,
            confirm_token=body.confirm_token,
            delete_junk=body.delete_junk,
            delete_corrupt=body.delete_corrupt,
            check_corrupt=body.check_corrupt,
        )
    except (ValueError, RuntimeError) as e:
        code = 409 if "already running" in str(e).lower() else 400
        raise HTTPException(code, str(e)) from e


@app.get("/api/cleanup/status")
def api_cleanup_status():
    return cleanup.get_cleanup_status()


@app.post("/api/cleanup/cancel")
def api_cleanup_cancel():
    return cleanup.request_cancel()


# ----- Library / browse -----


@app.get("/api/stats")
def api_stats():
    return scanner.library_stats()


@app.get("/api/folders")
def api_folders():
    return {"folders": scanner.list_folders()}


@app.get("/api/names")
def api_names(
    q: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    """Folder-path name index for Browse (not face People)."""
    return {"names": scanner.list_path_names(q=q, limit=limit)}


@app.get("/api/timeline")
def api_timeline():
    return {"months": scanner.list_timeline()}


@app.get("/api/photos")
def api_photos(
    folder: str | None = None,
    year: str | None = None,
    month: str | None = None,
    q: str | None = None,
    person_id: int | None = Query(None, ge=1),
    sort: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return scanner.list_photos(
        folder=folder,
        year=year,
        month=month,
        q=q,
        person_id=person_id,
        sort=sort,
        limit=limit,
        offset=offset,
    )


class PhotosRemoveBody(BaseModel):
    photo_ids: list[int] = Field(default_factory=list, min_length=1, max_length=500)
    mode: str = "quarantine"  # quarantine | delete
    confirm_token: str | None = None


@app.post("/api/photos/remove")
def api_photos_remove(body: PhotosRemoveBody):
    """Quarantine or permanently delete library photos selected in Browse."""
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    mode = (body.mode or "quarantine").strip().lower()
    if mode not in ("quarantine", "delete"):
        raise HTTPException(400, "mode must be quarantine or delete")
    if mode == "delete":
        from app.services.safety import CONFIRM_DELETE_FOREVER, require_confirm_token

        try:
            require_confirm_token(
                body.confirm_token,
                CONFIRM_DELETE_FOREVER,
                action="permanently delete library photos",
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    try:
        return dedupe.resolve_duplicates(
            None,
            body.photo_ids,
            mode=mode,
            delete_all=True,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1),
    year: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Search filename, folder, and rel_path; optional year filter from timeline."""
    return scanner.list_photos(q=q, year=year, limit=limit, offset=offset)


@app.get("/api/map/points")
def api_map_points(limit: int = Query(5000, ge=1, le=20000)):
    return {"points": scanner.list_map_points(limit=limit)}


@app.get("/api/smart-folders")
def api_smart_folders():
    return {"folders": smart_folders.list_smart_folders()}


@app.get("/api/smart-folders/{folder_id}/photos")
def api_smart_folder_photos(
    folder_id: str,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    data = smart_folders.list_smart_folder_photos(folder_id, limit=limit, offset=offset)
    if data is None:
        raise HTTPException(404, "Smart folder not found")
    return data


@app.get("/api/photos/{photo_id}")
def api_photo(photo_id: int):
    photo = scanner.get_photo(photo_id)
    if not photo:
        raise HTTPException(404, "Photo not found")
    return photo


@app.get("/api/photos/{photo_id}/thumb")
def api_thumb(photo_id: int):
    photo = scanner.get_photo_raw(photo_id)
    if not photo:
        raise HTTPException(404, "Thumbnail not found")
    try:
        path = scanner.ensure_library_thumb(photo_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"Could not build thumbnail: {e}") from e
    try:
        path = assert_under_root(Path(path), config.THUMBS_DIR)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not path.exists():
        raise HTTPException(404, "Thumbnail file missing")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/photos/{photo_id}/file")
def api_file(photo_id: int):
    photo = scanner.get_photo_raw(photo_id)
    if not photo:
        raise HTTPException(404, "Photo not found")
    try:
        root = get_photo_root()
        path = assert_under_root(Path(photo["path"]), root)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(path, filename=photo["filename"])


# ----- Scan -----


class ScanBody(BaseModel):
    full: bool = False


@app.get("/api/scan/status")
def api_scan_status():
    return scanner.get_scan_status()


@app.post("/api/scan/start")
def api_scan_start(body: ScanBody | None = None):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT in Settings first")
    try:
        return scanner.start_scan(full=bool(body.full) if body else False)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/scan/cancel")
def api_scan_cancel():
    scanner.request_cancel()
    return scanner.get_scan_status()


@app.post("/api/scan/pause")
def api_scan_pause():
    try:
        return scanner.request_pause()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/scan/resume")
def api_scan_resume():
    try:
        return scanner.request_resume()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


# ----- Duplicates -----


@app.get("/api/duplicates/exact")
def api_dupes_exact(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return dedupe.find_exact_duplicates(limit=limit, offset=offset)


@app.get("/api/duplicates/near")
def api_dupes_near(
    threshold: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return dedupe.find_near_duplicates(threshold=threshold, limit=limit, offset=offset)


class ResolveBody(BaseModel):
    keep_id: int | None = None
    delete_ids: list[int] = Field(default_factory=list, max_length=100)
    mode: str = "quarantine"  # quarantine | delete
    delete_all: bool = False


@app.post("/api/duplicates/resolve")
def api_dupes_resolve(body: ResolveBody):
    try:
        return dedupe.resolve_duplicates(
            body.keep_id,
            body.delete_ids,
            mode=body.mode,
            delete_all=body.delete_all,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class ExactBatchBody(BaseModel):
    mode: str = "delete"  # delete | quarantine
    confirm: bool = False


@app.get("/api/duplicates/exact/batch/preview")
def api_exact_batch_preview():
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    return dedupe.preview_exact_dedupe()


@app.get("/api/duplicates/exact/batch/status")
def api_exact_batch_status():
    return dedupe.get_exact_dedupe_batch_status()


@app.post("/api/duplicates/exact/batch/start")
def api_exact_batch_start(body: ExactBatchBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return dedupe.start_exact_dedupe_batch(mode=body.mode, confirm=body.confirm)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/duplicates/exact/batch/cancel")
def api_exact_batch_cancel():
    return dedupe.request_exact_dedupe_batch_cancel()


class QuarantineIdsBody(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=200)
    confirm_token: str | None = None


@app.get("/api/quarantine")
def api_quarantine_list(
    limit: int = Query(120, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return quarantine.list_quarantine(limit=limit, offset=offset)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/quarantine/{item_id}/thumb")
def api_quarantine_thumb(item_id: int):
    item = quarantine.get_quarantine_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    thumb = item.get("thumb_path")
    if thumb:
        try:
            path = assert_under_root(Path(thumb), config.THUMBS_DIR)
            if path.exists():
                return FileResponse(path, media_type="image/jpeg")
        except ValueError:
            pass
    # Fall back to original file (images) or 404
    try:
        path = assert_under_root(Path(item["quarantine_path"]), get_photo_root())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path)


@app.get("/api/quarantine/{item_id}/file")
def api_quarantine_file(item_id: int):
    item = quarantine.get_quarantine_item(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    try:
        path = assert_under_root(Path(item["quarantine_path"]), get_photo_root())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path)


@app.post("/api/quarantine/delete")
def api_quarantine_delete(body: QuarantineIdsBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    if not body.ids:
        raise HTTPException(400, "No ids provided")
    try:
        return quarantine.delete_quarantine_items(body.ids, confirm_token=body.confirm_token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/quarantine/restore")
def api_quarantine_restore(body: QuarantineIdsBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    if not body.ids:
        raise HTTPException(400, "No ids provided")
    try:
        return quarantine.restore_quarantine_items(body.ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ----- Import -----


class ImportPreviewBody(BaseModel):
    source_dir: str
    check_near: bool = True
    threshold: int | None = None


class ImportStartBody(BaseModel):
    source_dir: str
    dest_folder: str = "Inbox"
    check_near: bool = True
    threshold: int | None = None


@app.post("/api/import/preview")
def api_import_preview(body: ImportPreviewBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return importer.preview_import(
            body.source_dir, check_near=body.check_near, threshold=body.threshold
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/import/start")
def api_import_start(body: ImportStartBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return importer.start_import(
            body.source_dir,
            dest_folder=body.dest_folder,
            check_near=body.check_near,
            threshold=body.threshold,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/import/status")
def api_import_status():
    return importer.get_import_status()


@app.post("/api/import/cancel")
def api_import_cancel():
    importer.request_cancel()
    return importer.get_import_status()


@app.post("/api/import/pause")
def api_import_pause():
    try:
        return importer.request_import_pause()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/import/resume")
def api_import_resume():
    try:
        return importer.request_import_resume()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


# ----- iCloud Archive -----


class ICloudLoginBody(BaseModel):
    apple_id: str
    password: str
    account_id: int | None = None


class ICloud2FABody(BaseModel):
    code: str


class ICloudSmsBody(BaseModel):
    phone_id: int | str | None = None


class ICloudSelectAccountBody(BaseModel):
    account_id: int


class ArchiveFilterBody(BaseModel):
    keep_years: float | None = Field(default=None, ge=0.1, le=50)
    keep_months: float | None = Field(default=None, ge=1, le=600)
    older_than: str | None = None
    range_start: str | None = None
    range_end: str | None = None
    asset_ids: list[str] | None = Field(default=None, max_length=5000)


class ArchiveStartBody(ArchiveFilterBody):
    delete_after: bool = False
    confirm_delete: bool = False
    confirm_token: str | None = None
    dest_folder: str = "FromiCloud"
    check_near: bool = True


class ArchiveLargestBody(ArchiveFilterBody):
    entire_inventory: bool = False
    media: str = Field(default="all", pattern="^(all|video|photo)$")
    limit: int = Field(default=50, ge=1, le=200)


@app.get("/api/icloud/status")
def api_icloud_status():
    auth = icloud_client.auth_status()
    inv = icloud_archive.get_inventory_status()
    return {**auth, "inventory": inv, "archive": icloud_archive.get_archive_status()}


@app.get("/api/icloud/accounts")
def api_icloud_accounts():
    return {"accounts": icloud_client.list_accounts(), "active_id": icloud_client.get_active_account_id()}


@app.post("/api/icloud/accounts/select")
def api_icloud_accounts_select(body: ICloudSelectAccountBody):
    try:
        return icloud_client.set_active_account(body.account_id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


class ICloudAccountCreateBody(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    apple_id: str | None = None


class ICloudAccountUpdateBody(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=64)
    apple_id: str | None = None


@app.post("/api/icloud/accounts")
def api_icloud_accounts_create(body: ICloudAccountCreateBody):
    try:
        return icloud_client.create_account(body.label, apple_id=body.apple_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.patch("/api/icloud/accounts/{account_id}")
def api_icloud_accounts_update(account_id: int, body: ICloudAccountUpdateBody):
    try:
        return icloud_client.update_account(
            account_id, label=body.label, apple_id=body.apple_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/icloud/accounts/{account_id}")
def api_icloud_accounts_delete(account_id: int):
    try:
        return icloud_client.delete_account(account_id)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/auth")
def api_icloud_auth(body: ICloudLoginBody):
    try:
        icloud_client.ensure_icloud_gitignore()
        return icloud_client.login(
            body.apple_id, body.password, account_id=body.account_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/auth/2fa")
def api_icloud_2fa(body: ICloud2FABody):
    try:
        return icloud_client.submit_2fa(body.code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/auth/2fa/resend")
def api_icloud_2fa_resend():
    try:
        return icloud_client.resend_2fa()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/auth/2fa/sms")
def api_icloud_2fa_sms(body: ICloudSmsBody | None = None):
    try:
        return icloud_client.request_2fa_sms(phone_id=(body.phone_id if body else None))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/logout")
def api_icloud_logout():
    return icloud_client.logout()


@app.post("/api/icloud/auth/reset")
def api_icloud_auth_reset():
    """Clear stuck/expired 2FA session and return to a fresh login."""
    return icloud_client.reset_auth()


@app.get("/api/icloud/inventory/status")
def api_icloud_inventory_status():
    return icloud_archive.get_inventory_status()


@app.post("/api/icloud/inventory/refresh")
def api_icloud_inventory_refresh():
    try:
        return icloud_archive.start_inventory_refresh()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/inventory/cancel")
def api_icloud_inventory_cancel():
    return icloud_archive.request_inventory_cancel()


@app.post("/api/icloud/archive/estimate")
def api_icloud_archive_estimate(body: ArchiveFilterBody):
    try:
        return icloud_archive.estimate_archive(
            keep_years=body.keep_years,
            keep_months=body.keep_months,
            older_than=body.older_than,
            range_start=body.range_start,
            range_end=body.range_end,
            asset_ids=body.asset_ids,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/archive/assets")
def api_icloud_archive_assets(
    body: ArchiveFilterBody,
    limit: int = Query(120, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        return icloud_archive.list_matching_assets(
            keep_years=body.keep_years,
            keep_months=body.keep_months,
            older_than=body.older_than,
            range_start=body.range_start,
            range_end=body.range_end,
            asset_ids=body.asset_ids,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/archive/largest")
def api_icloud_archive_largest(body: ArchiveLargestBody):
    try:
        return icloud_archive.list_largest_assets(
            keep_years=body.keep_years,
            keep_months=body.keep_months,
            older_than=body.older_than,
            range_start=body.range_start,
            range_end=body.range_end,
            entire_inventory=body.entire_inventory,
            media=body.media,
            limit=body.limit,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/archive/start")
def api_icloud_archive_start(body: ArchiveStartBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT in Settings first")
    try:
        return icloud_archive.start_archive(
            keep_years=body.keep_years,
            keep_months=body.keep_months,
            older_than=body.older_than,
            range_start=body.range_start,
            range_end=body.range_end,
            asset_ids=body.asset_ids,
            delete_after=body.delete_after,
            dest_folder=body.dest_folder,
            check_near=body.check_near,
            confirm_delete=body.confirm_delete,
            confirm_token=body.confirm_token,
        )
    except (ValueError, RuntimeError) as e:
        code = 409 if "already running" in str(e).lower() else 400
        raise HTTPException(code, str(e)) from e


@app.get("/api/icloud/archive/status")
def api_icloud_archive_status():
    return icloud_archive.get_archive_status()


@app.post("/api/icloud/archive/cancel")
def api_icloud_archive_cancel():
    return icloud_archive.request_archive_cancel()


@app.post("/api/icloud/archive/pause")
def api_icloud_archive_pause():
    try:
        return icloud_archive.request_archive_pause()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/icloud/archive/resume")
def api_icloud_archive_resume():
    try:
        return icloud_archive.request_archive_resume()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/icloud/assets/{asset_id}/thumb")
def api_icloud_asset_thumb(asset_id: str):
    """Serve a cached iCloud preview thumb (downloads on first request)."""
    try:
        path = icloud_client.ensure_thumb(asset_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(401, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Thumb unavailable: {e}") from e
    if not path.exists():
        raise HTTPException(404, "Thumbnail missing")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/icloud/assets/{asset_id}/media")
def api_icloud_asset_media(asset_id: str, request: Request):
    """
    Stream iCloud original/medium bytes for in-browser preview (photos + videos).
    Supports HTTP Range so HTML5 video can seek.
    """
    range_header = request.headers.get("range")
    try:
        photo, version, upstream = icloud_client.open_asset_media_stream(
            asset_id, range_header=range_header, prefer_medium=True
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(401, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Media unavailable: {e}") from e

    filename = getattr(photo, "filename", None) or asset_id
    item_type = getattr(photo, "item_type", None)
    media_type = (
        upstream.headers.get("Content-Type")
        or icloud_client._guess_media_type(filename, item_type)
    )
    status = int(upstream.status_code)
    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=600",
        "X-iCloud-Version": version,
    }
    for key in ("Content-Length", "Content-Range"):
        if key in upstream.headers:
            out_headers[key] = upstream.headers[key]

    def _iter():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        _iter(),
        status_code=status,
        media_type=media_type,
        headers=out_headers,
    )


# ----- Organize -----


class MoveBody(BaseModel):
    photo_ids: list[int]
    dest_folder: str


class OrganizeDateBody(BaseModel):
    photo_ids: list[int] | None = None
    pattern: str = "YYYY/YYYY-MM"
    confirm: bool = False
    consolidate: bool = True
    undated_folder: str = "Undated"
    remove_empty_folders: bool = True


class PurgeInaccessibleBody(BaseModel):
    confirm: bool = False
    folder_prefix: str | None = "Google Drive Old"


class RawConvertStartBody(BaseModel):
    confirm: bool = False
    pattern: str = "YYYY/YYYY-MM"
    quarantine: bool = True


class FaceScanStartBody(BaseModel):
    confirm: bool = False
    limit: int | None = None


class NameClusterBody(BaseModel):
    cluster_key: str
    name: str


class CreatePersonBody(BaseModel):
    name: str


class AssignFaceBody(BaseModel):
    person_id: int | None = None
    ignore: bool = False


class IgnoreClusterBody(BaseModel):
    cluster_key: str


class MergePeopleBody(BaseModel):
    source_id: int
    target_id: int


class RenamePersonBody(BaseModel):
    name: str


class AcceptPersonMatchesBody(BaseModel):
    face_ids: list[int] = Field(default_factory=list)


class RenameBody(BaseModel):
    filename: str


@app.post("/api/organize/move")
def api_organize_move(body: MoveBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    return organize.move_photos(body.photo_ids, body.dest_folder)


@app.post("/api/organize/by-date")
def api_organize_by_date(body: OrganizeDateBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    if body.pattern not in ("YYYY/YYYY-MM", "YYYY-MM", "YYYY"):
        raise HTTPException(400, "Invalid pattern")
    try:
        return organize.organize_by_date(
            photo_ids=body.photo_ids,
            pattern=body.pattern,
            confirm=body.confirm,
            consolidate=body.consolidate,
            undated_folder=body.undated_folder,
            remove_empty_folders=body.remove_empty_folders,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/organize/status")
def api_organize_status():
    return organize.get_organize_status()


@app.post("/api/organize/cancel")
def api_organize_cancel():
    return organize.request_organize_cancel()


@app.post("/api/organize/pause")
def api_organize_pause():
    try:
        return organize.request_organize_pause()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/organize/resume")
def api_organize_resume():
    try:
        return organize.request_organize_resume()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/organize/inaccessible/preview")
def api_organize_inaccessible_preview(body: PurgeInaccessibleBody | None = None):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    prefix = body.folder_prefix if body else "Google Drive Old"
    return organize.preview_inaccessible(folder_prefix=prefix)


@app.post("/api/organize/inaccessible/purge")
def api_organize_inaccessible_purge(body: PurgeInaccessibleBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return organize.purge_inaccessible(
            folder_prefix=body.folder_prefix,
            confirm=body.confirm,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/raw/preview")
def api_raw_preview():
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    return raw_convert.preview_raw_convert()


@app.post("/api/raw/start")
def api_raw_start(body: RawConvertStartBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    if body.pattern not in ("YYYY/YYYY-MM", "YYYY-MM", "YYYY"):
        raise HTTPException(400, "Invalid pattern")
    try:
        return raw_convert.start_raw_convert(
            confirm=body.confirm,
            pattern=body.pattern,
            quarantine=body.quarantine,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/raw/status")
def api_raw_status():
    return raw_convert.get_raw_convert_status()


@app.post("/api/raw/cancel")
def api_raw_cancel():
    return raw_convert.request_raw_convert_cancel()


@app.post("/api/raw/pause")
def api_raw_pause():
    try:
        return raw_convert.request_raw_convert_pause()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/raw/resume")
def api_raw_resume():
    try:
        return raw_convert.request_raw_convert_resume()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/faces/preview")
def api_faces_preview():
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    return faces.face_scan_preview()


@app.post("/api/faces/scan/start")
def api_faces_scan_start(body: FaceScanStartBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return faces.start_face_scan(confirm=body.confirm, limit=body.limit)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/faces/scan/status")
def api_faces_scan_status():
    return faces.get_face_scan_status()


@app.post("/api/faces/scan/cancel")
def api_faces_scan_cancel():
    return faces.request_face_scan_cancel()


@app.post("/api/faces/scan/pause")
def api_faces_scan_pause():
    try:
        return faces.request_face_scan_pause()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/faces/scan/resume")
def api_faces_scan_resume():
    try:
        return faces.request_face_scan_resume()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/faces/cluster")
def api_faces_cluster():
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return faces.cluster_unknown_faces()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/faces/clusters")
def api_faces_clusters(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return faces.list_clusters(limit=limit, offset=offset)


@app.post("/api/faces/clusters/name")
def api_faces_name_cluster(body: NameClusterBody):
    try:
        return faces.name_cluster(body.cluster_key, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/faces/clusters/ignore")
def api_faces_ignore_cluster(body: IgnoreClusterBody):
    try:
        return faces.ignore_cluster(body.cluster_key)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/faces/clusters/{cluster_key}")
def api_faces_cluster_detail(cluster_key: str, limit: int = Query(48, ge=1, le=200)):
    data = faces.get_cluster(cluster_key, limit=limit)
    if not data:
        raise HTTPException(404, "Cluster not found")
    return data


@app.post("/api/faces/{face_id}/assign")
def api_faces_assign(face_id: int, body: AssignFaceBody):
    try:
        return faces.assign_face(face_id, body.person_id, ignore=body.ignore)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/faces/{face_id}/thumb")
def api_face_thumb(face_id: int):
    path = faces.face_thumb_path(face_id)
    if not path or not path.exists():
        raise HTTPException(404, "Face thumb not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/people")
def api_people():
    return {"people": faces.list_people()}


@app.post("/api/people")
def api_people_create(body: CreatePersonBody):
    try:
        return faces.create_person(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/people/merge")
def api_people_merge(body: MergePeopleBody):
    try:
        return faces.merge_people(body.source_id, body.target_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/people/{person_id}/photos")
def api_people_photos(
    person_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str | None = Query(None),
):
    data = faces.list_person_photos(
        person_id, limit=limit, offset=offset, sort=sort
    )
    if not data:
        raise HTTPException(404, "Person not found")
    return data


@app.post("/api/people/{person_id}/find-matches")
def api_people_find_matches(
    person_id: int,
    max_distance: float = Query(0.42, ge=0.05, le=0.8),
    limit: int = Query(120, ge=1, le=500),
):
    """Match unnamed faces to this person using their known face samples."""
    try:
        return faces.find_matches_for_person(
            person_id, max_distance=max_distance, limit=limit
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/people/{person_id}/accept-matches")
def api_people_accept_matches(person_id: int, body: AcceptPersonMatchesBody):
    try:
        return faces.accept_matches_for_person(person_id, body.face_ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/people/{person_id}/rename")
def api_people_rename(person_id: int, body: RenamePersonBody):
    try:
        return faces.rename_person(person_id, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/photos/{photo_id}/rename")
def api_rename(photo_id: int, body: RenameBody):
    if not photo_root_configured():
        raise HTTPException(400, "Set PHOTO_ROOT first")
    try:
        return organize.rename_photo(photo_id, body.filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ----- UI -----

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
