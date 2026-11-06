"""Smoke tests — no live iCloud / WD required."""

from __future__ import annotations

from datetime import datetime, timezone

from app.database import open_connection
from app.services import icloud_archive
from tests.helpers import seed_icloud_assets, utc_days_ago


def test_open_connection_sets_busy_timeout():
    conn = open_connection()
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert int(row[0]) >= 60000
    finally:
        conn.close()


def test_icloud_status_endpoint(client):
    r = client.get("/api/icloud/status")
    assert r.status_code == 200
    data = r.json()
    assert "connected" in data
    assert "inventory" in data
    assert "archive" in data
    assert "accounts" in data


def test_estimate_requires_filter(client):
    r = client.post("/api/icloud/archive/estimate", json={})
    assert r.status_code == 400


def test_estimate_older_than_keep_window(client):
    seed_icloud_assets(
        [
            (1, "a-old", "old.jpg", utc_days_ago(900), 1000, "image"),
            (1, "a-new", "new.jpg", utc_days_ago(30), 500, "image"),
            (1, "a-vid", "clip.mov", utc_days_ago(900), 9000, "video"),
        ]
    )

    est = icloud_archive.estimate_archive(keep_years=2)
    assert est["count"] == 2
    assert est["bytes"] == 10000
    assert est["photos"] == 1
    assert est["videos"] == 1

    r = client.post("/api/icloud/archive/estimate", json={"keep_years": 2})
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_estimate_selected_ids(client):
    seed_icloud_assets(
        [
            (1, "x1", "a.jpg", "2020-01-01T00:00:00+00:00", 10, "image"),
            (1, "x2", "b.jpg", "2020-01-02T00:00:00+00:00", 20, "image"),
        ]
    )
    est = icloud_archive.estimate_archive(asset_ids=["x2"])
    assert est["count"] == 1
    assert est["bytes"] == 20


def test_list_matching_assets_pagination(client):
    seed_icloud_assets(
        [
            (1, f"id-{i}", f"f{i}.jpg", "2019-06-01T00:00:00+00:00", 100, "image")
            for i in range(5)
        ]
    )
    r = client.post(
        "/api/icloud/archive/assets?limit=2&offset=0",
        json={"keep_years": 2},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 5
    assert len(data["assets"]) == 2


def test_archive_start_requires_photo_root(client):
    r = client.post(
        "/api/icloud/archive/start",
        json={"keep_years": 2, "delete_after": False},
    )
    assert r.status_code == 400


def test_auth_reset(client):
    r = client.post("/api/icloud/auth/reset", json={})
    assert r.status_code == 200
    assert r.json()["status"] == "logged_out"


def test_resolve_cutoff_years():
    cutoff = icloud_archive.resolve_cutoff(keep_years=2)
    assert cutoff is not None
    delta = datetime.now(timezone.utc) - cutoff
    assert 700 < delta.days < 800


def test_jobs_busy_endpoint(client):
    r = client.get("/api/jobs/busy")
    assert r.status_code == 200
    data = r.json()
    assert data["busy"] is False
    assert data["active"] is None
    assert "safe_while_busy" in data
    assert "blocked_while_busy" in data


def test_index_html_has_archive_tab(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b'data-view="archive"' in r.content
    assert b"btn-archive-load-grid" in r.content


def test_organize_status_has_progress_fields(client):
    r = client.get("/api/organize/status")
    assert r.status_code == 200
    data = r.json()
    for key in ("status", "processed", "total", "moved", "missing", "errors", "folders_removed"):
        assert key in data


def test_index_html_has_progress_bars(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.content
    assert b'id="org-bar"' in html
    assert b'id="org-consolidate"' in html
    assert b'id="btn-raw-start"' in html
    assert b'data-view="converter"' in html
    assert b'id="import-bar"' in html
    assert b'id="cleanup-bar"' in html
    assert b'id="inventory-bar"' in html
    assert b'id="inventory-progress"' in html
    assert b'id="job-busy-bar"' in html
    assert b'id="job-busy-pct"' in html
    assert b'id="btn-import-cancel"' in html
    assert b'id="btn-icloud-2fa-resend"' in html
    assert b'id="btn-icloud-2fa-sms"' in html
    assert b'id="icloud-2fa-phone"' in html
    assert b'id="icloud-2fa-phone-display"' in html
    assert b'id="btn-icloud-account-add"' in html
    assert b'id="btn-archive-largest"' in html
    assert b'id="archive-pick-panel"' in html
    assert b'id="tab-pick-largest"' in html
    assert b'id="archive-options"' in html
    assert b'id="btn-inventory-toggle"' in html
    assert b'id="btn-icloud-account-expand"' in html
    assert b'id="activity-toast"' in html