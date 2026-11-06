"""Path safety and archive cutoff helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.paths import assert_under_root
from app.services import icloud_archive


def test_assert_under_root_allows_child(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    child = root / "a" / "b.jpg"
    child.parent.mkdir(parents=True)
    child.write_bytes(b"x")
    out = assert_under_root(child, root)
    assert out == child.resolve() or out == Path(child)


def test_assert_under_root_rejects_escape(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    outside = tmp_path / "other" / "x.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        assert_under_root(outside, root)


def test_assert_under_root_rejects_dotdot(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    with pytest.raises(ValueError):
        assert_under_root(root / ".." / "escape.jpg", root)


def test_resolve_cutoff_months():
    cutoff = icloud_archive.resolve_cutoff(keep_months=6)
    assert cutoff is not None
    days = (datetime.now(timezone.utc) - cutoff).days
    assert 150 < days < 220


def test_resolve_cutoff_requires_one_window():
    assert icloud_archive.resolve_cutoff() is None
