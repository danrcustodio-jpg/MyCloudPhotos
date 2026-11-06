"""Shared pytest fixtures — offline, no live iCloud / WD."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Isolate DB/cache before app imports bind DATA_DIR
_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_testdata"
_TEST_DATA = _TEST_ROOT / "data"
os.environ["DATA_DIR"] = str(_TEST_DATA)
os.environ.pop("PHOTO_ROOT", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Per-test isolated DATA_DIR + SQLite + cleared in-memory iCloud session."""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.delenv("PHOTO_ROOT", raising=False)

    import app.config as config
    import app.database as database
    import app.services.icloud_archive as icloud_archive
    import app.services.icloud_client as icloud_client
    import app.services.jobs as jobs

    config.DATA_DIR = data
    config.DB_PATH = data / "index.db"
    config.THUMBS_DIR = data / "thumbs"
    config.ICLOUD_DIR = data / "icloud"
    config.ICLOUD_COOKIE_DIR = data / "icloud" / "cookies"
    config.ICLOUD_STAGING_DIR = data / "icloud_staging"
    config.ICLOUD_THUMBS_DIR = data / "icloud_thumbs"
    database.DB_PATH = config.DB_PATH
    config.ensure_data_dirs()
    init_db()

    with jobs._claim_lock:
        jobs._claimed_job_id = None

    with icloud_client._lock:
        icloud_client._api = None
        icloud_client._pending_2fa = False
        icloud_client._session_account_id = None
        icloud_client._2fa_code_requested = False
        icloud_client._last_2fa_error = None
        icloud_client._sms_phone_id = None

    with icloud_archive._inventory.lock:
        icloud_archive._inventory.status = "idle"
        icloud_archive._inventory.processed = 0
        icloud_archive._inventory.total_hint = 0
        icloud_archive._inventory.message = ""
        icloud_archive._inventory.cancel_requested = False

    with icloud_archive._archive.lock:
        icloud_archive._archive.status = "idle"
        icloud_archive._archive.job_id = None
        icloud_archive._archive.phase = ""
        icloud_archive._archive.processed = 0
        icloud_archive._archive.total = 0
        icloud_archive._archive.message = ""
        icloud_archive._archive.cancel_requested = False

    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def photo_root(tmp_path, monkeypatch):
    """Local temp tree as PHOTO_ROOT (no SMB)."""
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setenv("PHOTO_ROOT", str(root))
    from app.database import set_setting

    set_setting("photo_root", str(root))
    return root


@pytest.fixture
def jpeg_bytes():
    # Minimal JPEG (1x1) for library fixtures
    return bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xE0,
            0x00,
            0x10,
            0x4A,
            0x46,
            0x49,
            0x46,
            0x00,
            0x01,
            0x01,
            0x00,
            0x00,
            0x01,
            0x00,
            0x01,
            0x00,
            0x00,
            0xFF,
            0xDB,
            0x00,
            0x43,
            0x00,
            *([0x08] * 64),
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            0x00,
            0x01,
            0x00,
            0x01,
            0x01,
            0x01,
            0x11,
            0x00,
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x00,
            0x01,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x03,
            0xFF,
            0xDA,
            0x00,
            0x08,
            0x01,
            0x01,
            0x00,
            0x00,
            0x3F,
            0x00,
            0x7F,
            0xFF,
            0xD9,
        ]
    )
