"""Import allowlist, confirm tokens, job mutex, account CRUD — offline."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import icloud_archive, jobs, safety
from app.services.safety import (
    CONFIRM_CLEANUP,
    CONFIRM_DELETE_FOREVER,
    CONFIRM_DELETE_ICLOUD,
)


def test_require_confirm_token_rejects_mismatch():
    with pytest.raises(ValueError, match="DELETE FOREVER"):
        safety.require_confirm_token("wrong", CONFIRM_DELETE_FOREVER, action="delete")


def test_require_confirm_token_accepts_exact():
    safety.require_confirm_token(
        CONFIRM_DELETE_FOREVER, CONFIRM_DELETE_FOREVER, action="delete"
    )


def test_import_source_rejects_outside_allowlist(tmp_path, monkeypatch, photo_root):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.delenv("IMPORT_ALLOW_PREFIXES", raising=False)
    with pytest.raises(ValueError, match="outside allowed"):
        safety.assert_import_source_allowed(outside)


def test_import_source_allows_under_photo_root(photo_root):
    nested = photo_root / "Inbox" / "phone"
    nested.mkdir(parents=True)
    assert safety.assert_import_source_allowed(nested) == nested.resolve()


def test_import_source_allows_extra_prefix(tmp_path, monkeypatch):
    extra = tmp_path / "usb_dump"
    extra.mkdir()
    monkeypatch.setenv("IMPORT_ALLOW_PREFIXES", str(extra))
    nested = extra / "day1"
    nested.mkdir()
    assert safety.assert_import_source_allowed(nested) == nested.resolve()


def test_assert_can_start_claims_and_blocks_second():
    jobs.release_job(jobs.JOB_SCAN)
    jobs.release_job(jobs.JOB_IMPORT)
    jobs.assert_can_start(jobs.JOB_SCAN)
    with pytest.raises(RuntimeError, match="Another job"):
        jobs.assert_can_start(jobs.JOB_IMPORT)
    jobs.release_job(jobs.JOB_SCAN)
    jobs.assert_can_start(jobs.JOB_IMPORT)
    jobs.release_job(jobs.JOB_IMPORT)


def test_account_switch_blocked_during_inventory(client):
    with icloud_archive._inventory.lock:
        icloud_archive._inventory.status = "running"
        icloud_archive._inventory.message = "test"
    try:
        r = client.post("/api/icloud/accounts/select", json={"account_id": 2})
        assert r.status_code == 400
        assert "inventory" in r.json()["detail"].lower()
    finally:
        with icloud_archive._inventory.lock:
            icloud_archive._inventory.status = "idle"
            icloud_archive._inventory.message = ""


def test_cleanup_requires_confirm_token(client, photo_root):
    r = client.post(
        "/api/cleanup/start",
        json={"confirm": True, "delete_junk": True, "delete_corrupt": False},
    )
    assert r.status_code == 400
    assert "DELETE FILES" in r.json()["detail"]


def test_quarantine_delete_requires_confirm_token(client, photo_root):
    r = client.post(
        "/api/quarantine/delete",
        json={"ids": [1], "confirm_token": "nope"},
    )
    assert r.status_code == 400
    assert CONFIRM_DELETE_FOREVER in r.json()["detail"]


def test_archive_delete_requires_confirm_token(client, photo_root):
    """delete_after without exact token fails after claim is released."""
    with patch.object(
        icloud_archive,
        "estimate_archive",
        return_value={
            "count": 1,
            "bytes": 100,
            "estimated_batches": 1,
            "cutoff": None,
            "range_start": None,
            "range_end": None,
        },
    ):
        r = client.post(
            "/api/icloud/archive/start",
            json={
                "delete_after": True,
                "confirm_delete": True,
                "confirm_token": "wrong",
                "keep_years": 2,
            },
        )
    assert r.status_code == 400
    assert CONFIRM_DELETE_ICLOUD in r.json()["detail"]
    # Claim must not stick after failed start
    jobs.assert_can_start(jobs.JOB_SCAN)
    jobs.release_job(jobs.JOB_SCAN)


def test_account_create_rename_delete(client):
    create = client.post("/api/icloud/accounts", json={"label": "Avery"})
    assert create.status_code == 200
    new_id = create.json()["account"]["id"]
    labels = {a["label"] for a in create.json()["accounts"]}
    assert "Avery" in labels

    patch_r = client.patch(
        f"/api/icloud/accounts/{new_id}", json={"label": "Avery Renamed"}
    )
    assert patch_r.status_code == 200
    assert patch_r.json()["account"]["label"] == "Avery Renamed"

    delete = client.delete(f"/api/icloud/accounts/{new_id}")
    assert delete.status_code == 200
    assert new_id == delete.json()["deleted_id"]
    assert "Avery Renamed" not in {a["label"] for a in delete.json()["accounts"]}


def test_cannot_delete_last_account(client):
    accounts = client.get("/api/icloud/accounts").json()["accounts"]
    # Delete down to one
    for a in accounts[1:]:
        r = client.delete(f"/api/icloud/accounts/{a['id']}")
        assert r.status_code == 200
    last_id = client.get("/api/icloud/accounts").json()["accounts"][0]["id"]
    r = client.delete(f"/api/icloud/accounts/{last_id}")
    assert r.status_code == 400
    assert "last" in r.json()["detail"].lower()


def test_confirm_constants_match_docs():
    assert CONFIRM_DELETE_ICLOUD == "DELETE FROM ICLOUD"
    assert CONFIRM_DELETE_FOREVER == "DELETE FOREVER"
    assert CONFIRM_CLEANUP == "DELETE FILES"
