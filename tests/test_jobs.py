"""Job busy aggregator — offline with patched job status getters."""

from __future__ import annotations

from unittest.mock import patch

from app.services import jobs


def test_busy_idle_payload(client):
    r = client.get("/api/jobs/busy")
    assert r.status_code == 200
    data = r.json()
    assert data["busy"] is False
    assert data["active"] is None
    assert isinstance(data["jobs"], list)


def test_busy_scan_includes_progress_fields(client):
    scan = {
        "status": "running",
        "phase": "scanning",
        "total": 1000,
        "processed": 250,
        "message": "Scanning... 250 files",
        "paused": False,
        "eta_seconds": 120,
        "eta_label": "~2m",
        "rate_per_sec": 5.0,
        "added": 10,
        "updated": 2,
        "errors": 0,
    }
    with (
        patch("app.services.scanner.get_scan_status", return_value=scan),
        patch("app.services.importer.get_import_status", return_value={"status": "idle"}),
        patch(
            "app.services.icloud_archive.get_inventory_status",
            return_value={"status": "idle"},
        ),
        patch(
            "app.services.icloud_archive.get_archive_status",
            return_value={"status": "idle"},
        ),
        patch(
            "app.services.dedupe.get_exact_dedupe_batch_status",
            return_value={"status": "idle"},
        ),
        patch("app.services.cleanup.get_cleanup_status", return_value={"status": "idle"}),
        patch("app.services.organize.get_organize_status", return_value={"status": "idle"}),
        patch(
            "app.services.raw_convert.get_raw_convert_status",
            return_value={"status": "idle"},
        ),
        patch("app.services.faces.get_face_scan_status", return_value={"status": "idle"}),
    ):
        data = jobs.get_busy_status()

    assert data["busy"] is True
    active = data["active"]
    assert active is not None
    assert active["id"] == "scan"
    assert active["percent"] == 25
    assert active["unit"] == "files"
    assert active["phase_label"] == "Scanning"
    assert active["eta_label"]
    assert active["processed"] == 250
    assert active["total"] == 1000
    assert len(data["jobs"]) == 1


def test_busy_reports_first_of_multiple(client):
    """Soft mutex may allow races; aggregator still exposes jobs[0] as active."""
    with (
        patch(
            "app.services.scanner.get_scan_status",
            return_value={
                "status": "running",
                "phase": "scanning",
                "total": 10,
                "processed": 1,
                "message": "scan",
                "paused": False,
            },
        ),
        patch(
            "app.services.importer.get_import_status",
            return_value={
                "status": "running",
                "total": 5,
                "processed": 2,
                "message": "import",
                "copied": 2,
                "skipped_exact": 0,
                "skipped_near": 0,
                "errors": 0,
            },
        ),
        patch(
            "app.services.icloud_archive.get_inventory_status",
            return_value={"status": "idle"},
        ),
        patch(
            "app.services.icloud_archive.get_archive_status",
            return_value={"status": "idle"},
        ),
        patch(
            "app.services.dedupe.get_exact_dedupe_batch_status",
            return_value={"status": "idle"},
        ),
        patch("app.services.cleanup.get_cleanup_status", return_value={"status": "idle"}),
        patch("app.services.organize.get_organize_status", return_value={"status": "idle"}),
        patch(
            "app.services.raw_convert.get_raw_convert_status",
            return_value={"status": "idle"},
        ),
        patch("app.services.faces.get_face_scan_status", return_value={"status": "idle"}),
    ):
        data = jobs.get_busy_status()

    assert data["busy"] is True
    assert len(data["jobs"]) >= 2
    assert data["active"]["id"] == data["jobs"][0]["id"]
