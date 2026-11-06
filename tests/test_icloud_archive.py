"""iCloud archive / inventory — offline DB + mocked busy guards."""

from __future__ import annotations

from unittest.mock import patch

from app.services import icloud_archive
from tests.helpers import seed_icloud_assets, utc_days_ago


def test_inventory_status_shape(client):
    r = client.get("/api/icloud/inventory/status")
    assert r.status_code == 200
    data = r.json()
    for key in ("status", "processed", "total", "percent", "phase", "unit", "message"):
        assert key in data
    assert data["unit"] == "assets"


def test_inventory_status_completed_total_from_cache(client):
    seed_icloud_assets(
        [
            (1, "a1", "a.jpg", utc_days_ago(400), 100, "image"),
            (1, "a2", "b.jpg", utc_days_ago(500), 200, "image"),
        ]
    )
    with icloud_archive._inventory.lock:
        icloud_archive._inventory.status = "completed"
        icloud_archive._inventory.processed = 2
        icloud_archive._inventory.total_hint = 0
        icloud_archive._inventory.message = "ready"

    data = icloud_archive.get_inventory_status()
    assert data["asset_count"] == 2
    assert data["total"] == 2
    assert data["percent"] == 100


def test_estimate_scoped_to_active_account(client):
    seed_icloud_assets(
        [
            (1, "dan-old", "d.jpg", utc_days_ago(900), 1000, "image"),
            (2, "jess-old", "j.jpg", utc_days_ago(900), 5000, "image"),
        ]
    )
    # Active defaults to Dan (1)
    est = icloud_archive.estimate_archive(keep_years=2)
    assert est["count"] == 1
    assert est["bytes"] == 1000

    client.post("/api/icloud/accounts/select", json={"account_id": 2})
    est2 = icloud_archive.estimate_archive(keep_years=2)
    assert est2["count"] == 1
    assert est2["bytes"] == 5000


def test_archive_start_rejected_when_busy(client, photo_root):
    seed_icloud_assets(
        [(1, "old1", "o.jpg", utc_days_ago(900), 100, "image")]
    )

    def _busy(_job_id):
        raise RuntimeError("Another job is running (Library scan).")

    with patch("app.services.jobs.assert_can_start", side_effect=_busy):
        r = client.post(
            "/api/icloud/archive/start",
            json={"keep_years": 2, "delete_after": False, "confirm_delete": False},
        )
    assert r.status_code in (400, 409)
    detail = str(r.json().get("detail", "")).lower()
    assert "running" in detail or "job" in detail


def test_archive_status_idle_shape(client):
    r = client.get("/api/icloud/archive/status")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "idle"
    assert "processed" in data
    assert "phase" in data


def test_list_largest_orders_by_size_and_respects_keep_window(client):
    seed_icloud_assets(
        [
            (1, "big-old", "huge.mov", utc_days_ago(900), 50_000_000, "video"),
            (1, "mid-old", "mid.jpg", utc_days_ago(800), 5_000_000, "image"),
            (1, "tiny-old", "tiny.jpg", utc_days_ago(750), 100_000, "image"),
            (1, "big-recent", "recent.mov", utc_days_ago(30), 90_000_000, "video"),
        ]
    )
    data = icloud_archive.list_largest_assets(keep_years=2, limit=10)
    ids = [a["asset_id"] for a in data["assets"]]
    assert ids == ["big-old", "mid-old", "tiny-old"]
    assert data["total_matching"] == 3
    assert data["bytes_listed"] == 50_000_000 + 5_000_000 + 100_000
    assert data["entire_inventory"] is False
    assert "big-recent" not in ids


def test_list_largest_entire_inventory_and_media_filter(client):
    seed_icloud_assets(
        [
            (1, "v1", "a.mov", utc_days_ago(100), 8_000_000, "video"),
            (1, "p1", "b.jpg", utc_days_ago(100), 3_000_000, "image"),
            (1, "v2", "c.mov", utc_days_ago(50), 12_000_000, "video"),
        ]
    )
    all_data = icloud_archive.list_largest_assets(entire_inventory=True, limit=10)
    assert [a["asset_id"] for a in all_data["assets"]] == ["v2", "v1", "p1"]

    videos = icloud_archive.list_largest_assets(
        entire_inventory=True, media="video", limit=10
    )
    assert [a["asset_id"] for a in videos["assets"]] == ["v2", "v1"]
    assert videos["media"] == "video"

    photos = icloud_archive.list_largest_assets(
        entire_inventory=True, media="photo", limit=10
    )
    assert [a["asset_id"] for a in photos["assets"]] == ["p1"]


def test_list_largest_api_endpoint(client):
    seed_icloud_assets(
        [(1, "x1", "x.jpg", utc_days_ago(900), 999, "image")]
    )
    r = client.post(
        "/api/icloud/archive/largest",
        json={"keep_years": 2, "media": "all", "limit": 25},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["assets"][0]["asset_id"] == "x1"
    assert data["limit"] == 25

    bad = client.post("/api/icloud/archive/largest", json={})
    assert bad.status_code == 400
