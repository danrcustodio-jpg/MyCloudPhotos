"""Library / settings endpoints — temp PHOTO_ROOT, no NAS."""

from __future__ import annotations

from app.database import connect


def test_settings_get(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert "configured" in data
    assert "scan_workers" in data


def test_stats_empty_library(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["photo_count"] == 0


def test_folders_and_timeline_empty(client):
    assert client.get("/api/folders").status_code == 200
    assert client.get("/api/timeline").status_code == 200
    assert client.get("/api/names").status_code == 200


def test_photos_list_seeded(client, photo_root, jpeg_bytes):
    rel = "Inbox/seed.jpg"
    path = photo_root / "Inbox"
    path.mkdir(parents=True)
    file_path = path / "seed.jpg"
    file_path.write_bytes(jpeg_bytes)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO photos(
                path, rel_path, filename, folder, size, mtime, content_hash, phash,
                media_type, indexed_at
            ) VALUES (?, ?, 'seed.jpg', 'Inbox', ?, ?, 'hash1', NULL, 'image', datetime('now'))
            """,
            (str(file_path), rel, len(jpeg_bytes), file_path.stat().st_mtime),
        )

    r = client.get("/api/photos?limit=10&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert any(i.get("filename") == "seed.jpg" for i in data["items"])


def test_browse_by_name_from_folder_paths(client, photo_root, jpeg_bytes):
    rows = [
        ("Family/Jessica/2020/a.jpg", "Family/Jessica/2020", "a.jpg"),
        ("Family/Jessica/Trips/b.jpg", "Family/Jessica/Trips", "b.jpg"),
        ("Family/Graham/c.jpg", "Family/Graham", "c.jpg"),
        ("FromiCloud/Jess/d.jpg", "FromiCloud/Jess", "d.jpg"),
    ]
    with connect() as conn:
        for i, (rel, folder, filename) in enumerate(rows):
            path = photo_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg_bytes)
            conn.execute(
                """
                INSERT INTO photos(
                    path, rel_path, filename, folder, size, mtime, content_hash, phash,
                    media_type, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'image', datetime('now'))
                """,
                (
                    str(path),
                    rel,
                    filename,
                    folder,
                    len(jpeg_bytes),
                    path.stat().st_mtime,
                    f"hash-name-{i}",
                ),
            )

    r = client.get("/api/names")
    assert r.status_code == 200
    names = {n["name"]: n["count"] for n in r.json()["names"]}
    assert names.get("Jessica") == 2
    assert names.get("Graham") == 1
    assert names.get("Jess") == 1
    assert "FromiCloud" not in names
    assert "2020" not in names

    filtered = client.get("/api/names?q=jess")
    assert filtered.status_code == 200
    got = {n["name"] for n in filtered.json()["names"]}
    assert got == {"Jessica", "Jess"}

    photos = client.get("/api/photos?q=Jessica&limit=50")
    assert photos.status_code == 200
    assert photos.json()["total"] == 2


def test_map_points_empty(client):
    r = client.get("/api/map/points?limit=10")
    assert r.status_code == 200
    assert "points" in r.json()


def test_photos_person_filter_and_sort(client, photo_root, jpeg_bytes):
    """Browse by face person_id + sort order (date / name)."""
    rows = [
        ("Family/a.jpg", "Family", "zeta.jpg", "2020-01-01T12:00:00"),
        ("Family/b.jpg", "Family", "alpha.jpg", "2022-06-15T12:00:00"),
        ("Family/c.jpg", "Family", "middle.jpg", "2021-03-10T12:00:00"),
        ("Other/d.jpg", "Other", "other.jpg", "2023-01-01T12:00:00"),
    ]
    photo_ids: list[int] = []
    with connect() as conn:
        for i, (rel, folder, filename, exif) in enumerate(rows):
            path = photo_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg_bytes)
            cur = conn.execute(
                """
                INSERT INTO photos(
                    path, rel_path, filename, folder, size, mtime, content_hash, phash,
                    media_type, exif_date, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'image', ?, datetime('now'))
                """,
                (
                    str(path),
                    rel,
                    filename,
                    folder,
                    len(jpeg_bytes),
                    path.stat().st_mtime,
                    f"hash-person-{i}",
                    exif,
                ),
            )
            photo_ids.append(int(cur.lastrowid))
        cur = conn.execute("INSERT INTO people(name) VALUES (?)", ("Jessica",))
        person_id = int(cur.lastrowid)
        emb = b"\x00" * 16
        for pid in photo_ids[:3]:
            conn.execute(
                """
                INSERT INTO faces(
                    photo_id, person_id, x, y, w, h, det_score, embedding, ignored
                ) VALUES (?, ?, 0.1, 0.1, 0.2, 0.2, 0.9, ?, 0)
                """,
                (pid, person_id, emb),
            )

    r = client.get(f"/api/photos?person_id={person_id}&sort=date_desc&limit=50")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert data["sort"] == "date_desc"
    assert data["person_id"] == person_id
    assert [i["filename"] for i in data["items"]] == [
        "alpha.jpg",
        "middle.jpg",
        "zeta.jpg",
    ]

    by_name = client.get(f"/api/photos?person_id={person_id}&sort=name_asc&limit=50")
    assert by_name.status_code == 200
    assert [i["filename"] for i in by_name.json()["items"]] == [
        "alpha.jpg",
        "middle.jpg",
        "zeta.jpg",
    ]

    oldest = client.get(f"/api/photos?person_id={person_id}&sort=date_asc&limit=50")
    assert [i["filename"] for i in oldest.json()["items"]] == [
        "zeta.jpg",
        "middle.jpg",
        "alpha.jpg",
    ]

    people_photos = client.get(
        f"/api/people/{person_id}/photos?sort=name_desc&limit=50"
    )
    assert people_photos.status_code == 200
    pdata = people_photos.json()
    assert pdata["total"] == 3
    assert pdata["sort"] == "name_desc"
    assert [i["filename"] for i in pdata["items"]] == [
        "zeta.jpg",
        "middle.jpg",
        "alpha.jpg",
    ]


def test_thumb_regenerates_when_file_missing(client, photo_root, jpeg_bytes):
    """Missing thumb JPEG is rebuilt from the original on request."""
    rel = "Inbox/regen.jpg"
    path = photo_root / "Inbox"
    path.mkdir(parents=True)
    file_path = path / "regen.jpg"
    file_path.write_bytes(jpeg_bytes)

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO photos(
                path, rel_path, filename, folder, size, mtime, content_hash, phash,
                media_type, thumb_path, indexed_at
            ) VALUES (?, ?, 'regen.jpg', 'Inbox', ?, ?, 'hash-regen', NULL, 'image', ?, datetime('now'))
            """,
            (
                str(file_path),
                rel,
                len(jpeg_bytes),
                file_path.stat().st_mtime,
                str(photo_root / "missing" / "hash-regen.jpg"),
            ),
        )
        photo_id = int(cur.lastrowid)

    r = client.get(f"/api/photos/{photo_id}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert len(r.content) > 0

    # Second hit serves the cached file
    r2 = client.get(f"/api/photos/{photo_id}/thumb")
    assert r2.status_code == 200
    assert r2.content == r.content


def test_smart_folders_list(client):
    r = client.get("/api/smart-folders")
    assert r.status_code == 200
    assert "folders" in r.json()


def test_photos_remove_quarantine(client, photo_root, jpeg_bytes):
    path = photo_root / "Inbox"
    path.mkdir(parents=True)
    file_path = path / "bye.jpg"
    file_path.write_bytes(jpeg_bytes)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO photos(
                path, rel_path, filename, folder, size, mtime, content_hash, phash,
                media_type, indexed_at
            ) VALUES (?, 'Inbox/bye.jpg', 'bye.jpg', 'Inbox', ?, ?, 'hash-bye', NULL, 'image', datetime('now'))
            """,
            (str(file_path), len(jpeg_bytes), file_path.stat().st_mtime),
        )
        photo_id = int(cur.lastrowid)

    r = client.post(
        "/api/photos/remove",
        json={"photo_ids": [photo_id], "mode": "quarantine"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["removed_count"] == 1
    assert data["mode"] == "quarantine"
    assert not file_path.exists()
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM photos WHERE id=?", (photo_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 1


def test_date_backfill_from_folder_and_mtime_sort(client, photo_root, jpeg_bytes):
    from app.services.date_backfill import infer_date_from_paths

    assert infer_date_from_paths(folder="2026/2026-02", filename="a.jpg")["source"] == (
        "folder_month"
    )
    assert infer_date_from_paths(
        folder="", filename="Screenshot_20221112-101659_Chrome.jpg"
    )["exif_date"].startswith("2022-11-12")

    # mtimes in Feb/Mar 2026 (for Added-year / Added-month filters)
    from datetime import datetime

    m_a = datetime(2026, 2, 10).timestamp()
    m_b = datetime(2026, 2, 5).timestamp()
    m_c = datetime(2026, 3, 1).timestamp()
    rows = [
        ("2026/2026-02/a.jpg", "2026/2026-02", "a.jpg", m_a),
        ("2026/2026-02/b.jpg", "2026/2026-02", "b.jpg", m_b),
        ("Inbox/c.jpg", "Inbox", "c.jpg", m_c),
    ]
    with connect() as conn:
        for i, (rel, folder, filename, mtime) in enumerate(rows):
            path = photo_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(jpeg_bytes)
            conn.execute(
                """
                INSERT INTO photos(
                    path, rel_path, filename, folder, size, mtime, content_hash, phash,
                    media_type, exif_date, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'image', NULL, datetime('now'))
                """,
                (
                    str(path),
                    rel,
                    filename,
                    folder,
                    len(jpeg_bytes),
                    mtime,
                    f"hash-date-{i}",
                ),
            )

    preview = client.get("/api/dates/backfill/preview")
    assert preview.status_code == 200
    pdata = preview.json()
    assert pdata["would_update"] >= 2

    applied = client.post("/api/dates/backfill")
    assert applied.status_code == 200
    assert applied.json()["updated"] >= 2

    year = client.get("/api/photos?year=2026&sort=mtime_desc&limit=50")
    assert year.status_code == 200
    names = [i["filename"] for i in year.json()["items"]]
    assert "a.jpg" in names and "b.jpg" in names and "c.jpg" in names

    feb = client.get("/api/photos?year=2026&month=02&sort=mtime_desc&limit=50")
    assert feb.status_code == 200
    assert {i["filename"] for i in feb.json()["items"]} >= {"a.jpg", "b.jpg"}
    assert "c.jpg" not in [i["filename"] for i in feb.json()["items"]]

    by_mtime = client.get("/api/photos?sort=mtime_desc&limit=50")
    assert by_mtime.status_code == 200
    assert by_mtime.json()["sort"] == "mtime_desc"
    assert [i["filename"] for i in by_mtime.json()["items"][:3]] == [
        "c.jpg",
        "a.jpg",
        "b.jpg",
    ]


def test_video_thumb_extracts_frame(client, photo_root, tmp_path):
    """Library video thumbs use a real frame when ffmpeg is available."""
    import subprocess

    from PIL import Image

    from app.services.hashing import (
        _ffmpeg_exe,
        is_video_placeholder_thumb,
        make_video_placeholder,
        make_video_thumbnail,
    )

    ff = _ffmpeg_exe()
    if not ff:
        import pytest

        pytest.skip("ffmpeg not available")

    video = photo_root / "Inbox" / "clip.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ff,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(video),
        ],
        check=True,
        timeout=30,
        capture_output=True,
    )

    dest = tmp_path / "frame.jpg"
    make_video_thumbnail(dest, video_path=video)
    assert dest.exists() and dest.stat().st_size > 0
    assert not is_video_placeholder_thumb(dest)
    with Image.open(dest) as im:
        # Should not be the solid square placeholder look — red-ish frame
        px = im.convert("RGB").getpixel((im.size[0] // 2, im.size[1] // 2))
        assert px[0] > 150 and px[1] < 80

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO photos(
                path, rel_path, filename, folder, size, mtime, content_hash, phash,
                media_type, indexed_at
            ) VALUES (?, 'Inbox/clip.mp4', 'clip.mp4', 'Inbox', ?, ?, 'hash-vid', NULL, 'video', datetime('now'))
            """,
            (str(video), video.stat().st_size, video.stat().st_mtime),
        )
        photo_id = int(cur.lastrowid)

    # Seed a placeholder at the expected thumb path so ensure upgrades it
    from app.services.scanner import _thumb_path_for, ensure_library_thumb

    thumb = _thumb_path_for("hash-vid")
    make_video_placeholder(thumb)
    assert is_video_placeholder_thumb(thumb)
    with connect() as conn:
        conn.execute(
            "UPDATE photos SET thumb_path = ? WHERE id = ?",
            (str(thumb), photo_id),
        )

    out = ensure_library_thumb(photo_id)
    assert out.exists()
    assert not is_video_placeholder_thumb(out)

    r = client.get(f"/api/photos/{photo_id}/thumb")
    assert r.status_code == 200
    assert len(r.content) > 1000
