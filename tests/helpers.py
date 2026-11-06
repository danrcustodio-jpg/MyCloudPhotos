"""Shared offline test helpers (importable; not fixtures)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.database import connect


def seed_icloud_assets(rows: list[tuple]) -> None:
    """
    Insert icloud_assets rows.
    Each tuple: (account_id, asset_id, filename, created_at, size, item_type)
    """
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO icloud_assets(
                    account_id, asset_id, filename, created_at, size, item_type
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                row,
            )


def utc_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def make_mock_phone(*, device_id: int = 1):
    """Minimal trusted-phone object for SMS 2FA tests."""
    phone = MagicMock()
    phone.device_id = device_id
    phone.as_phone_number_payload.return_value = {"id": device_id}
    return phone


def make_mock_icloud_api(*, requires_2fa: bool = False, trusted: bool = True) -> MagicMock:
    """Fake PyiCloudService for auth unit tests."""
    api = MagicMock()
    api.requires_2fa = requires_2fa
    api.requires_2sa = False
    api.is_trusted_session = trusted and not requires_2fa
    api.request_2fa_code.return_value = True
    api.two_factor_delivery_method = "trusted_device"
    api.two_factor_delivery_notice = None
    api.validate_2fa_code.return_value = True
    api.trust_session.return_value = True
    return api
