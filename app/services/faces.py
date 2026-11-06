"""Local face detection, clustering, and people naming (InsightFace)."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.config import FACES_DIR, INSIGHTFACE_ROOT, ensure_data_dirs
from app.database import connect, rows_to_dicts, strip_photo_paths
from app.paths import assert_under_root
from app.services.hashing import open_image

# Cosine distance thresholds (embeddings are L2-normalized)
_AUTO_ASSIGN_DISTANCE = 0.40
_CLUSTER_DISTANCE = 0.45
_MIN_FACE_PX = 40
_FACE_THUMB_SIZE = 160
_DET_SIZE = (640, 640)

_face_app = None
_face_app_lock = threading.Lock()


@dataclass
class FaceScanState:
    status: str = "idle"
    phase: str = ""
    total: int = 0
    processed: int = 0
    faces_found: int = 0
    auto_assigned: int = 0
    errors: int = 0
    images_with_faces: int = 0
    message: str = ""
    started_at: float | None = None
    pause_started_at: float | None = None
    paused_sec: float = 0.0
    current_photo: dict[str, Any] | None = None
    recent_faces: list[dict[str, Any]] = field(default_factory=list)
    recent_photos: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    pause_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = FaceScanState()
_RECENT_FACE_LIMIT = 48
_RECENT_PHOTO_LIMIT = 24


def get_face_scan_status() -> dict[str, Any]:
    with _state.lock:
        elapsed = None
        rate = None
        paused = bool(_state.pause_requested) and _state.status == "running"
        paused_sec = _state.paused_sec
        if paused and _state.pause_started_at:
            paused_sec += max(0.0, time.time() - _state.pause_started_at)
        if _state.started_at and _state.processed > 0:
            elapsed = max(0.001, time.time() - _state.started_at - paused_sec)
            rate = _state.processed / elapsed
        pct = None
        if _state.total and _state.total > 0:
            pct = min(100, round(100 * _state.processed / _state.total))
        pending = max(0, int(_state.total) - int(_state.processed)) if _state.total else 0
        return {
            "status": _state.status,
            "phase": "paused" if paused else _state.phase,
            "total": _state.total,
            "processed": _state.processed,
            "pending": pending,
            "faces_found": _state.faces_found,
            "auto_assigned": _state.auto_assigned,
            "errors": _state.errors,
            "images_with_faces": _state.images_with_faces,
            "message": _state.message,
            "cancel_requested": _state.cancel_requested,
            "paused": paused,
            "percent": pct,
            "elapsed_sec": round(elapsed) if elapsed is not None else None,
            "images_per_sec": round(rate, 2) if rate is not None else None,
            "eta_sec": (
                int(pending / rate) if rate and pending > 0 else None
            ),
            "current_photo": dict(_state.current_photo) if _state.current_photo else None,
            "recent_faces": list(_state.recent_faces),
            "recent_photos": list(_state.recent_photos),
        }


def request_face_scan_cancel() -> dict[str, Any]:
    with _state.lock:
        if _state.status == "running":
            _state.cancel_requested = True
            _state.pause_requested = False
            _state.phase = "cancelling"
            _state.message = "Cancelling… finishing current photo"
        else:
            _state.cancel_requested = True
            _state.message = "Cancel requested…"
    return get_face_scan_status()


def request_face_scan_pause() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No face scan is running")
        if _state.cancel_requested:
            raise RuntimeError("Face scan is cancelling")
        if not _state.pause_requested:
            _state.pause_requested = True
            _state.pause_started_at = time.time()
            _state.phase = "paused"
            _state.message = "Paused — resume when ready (current photo will finish)"
    return get_face_scan_status()


def request_face_scan_resume() -> dict[str, Any]:
    with _state.lock:
        if _state.status != "running":
            raise RuntimeError("No face scan is running")
        if _state.pause_requested:
            if _state.pause_started_at:
                _state.paused_sec += max(0.0, time.time() - _state.pause_started_at)
            _state.pause_started_at = None
            _state.pause_requested = False
            _state.phase = "scanning"
            _state.message = "Resuming face scan…"
    return get_face_scan_status()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pack_embedding(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _unpack_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n <= 1e-12:
        return v
    return v / n


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b))


def _get_face_app():
    global _face_app
    with _face_app_lock:
        if _face_app is not None:
            return _face_app
        ensure_data_dirs()
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_s",
            root=str(INSIGHTFACE_ROOT),
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=-1, det_size=_DET_SIZE)
        _face_app = app
        return _face_app


def _load_bgr(path: Path) -> np.ndarray | None:
    try:
        with open_image(path) as img:
            rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    except Exception:
        return None
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        return None
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _save_face_thumb(bgr: np.ndarray, bbox: np.ndarray, dest: Path) -> None:
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    # Pad ~15%
    bw, bh = x2 - x1, y2 - y1
    x1 = max(0, int(x1 - 0.15 * bw))
    y1 = max(0, int(y1 - 0.15 * bh))
    x2 = min(w, int(x2 + 0.15 * bw))
    y2 = min(h, int(y2 + 0.15 * bh))
    if x2 <= x1 or y2 <= y1:
        return
    crop_bgr = bgr[y1:y2, x1:x2]
    crop_rgb = crop_bgr[:, :, ::-1]
    im = Image.fromarray(crop_rgb)
    im.thumbnail((_FACE_THUMB_SIZE, _FACE_THUMB_SIZE), Image.Resampling.BICUBIC)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, format="JPEG", quality=85)


def _person_centroids() -> list[tuple[int, np.ndarray]]:
    """Return (person_id, L2-normalized mean embedding) for named people with faces."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT person_id, embedding
            FROM faces
            WHERE person_id IS NOT NULL AND ignored = 0 AND embedding IS NOT NULL
            """
        ).fetchall()
    by_person: dict[int, list[np.ndarray]] = {}
    for row in rows:
        pid = int(row["person_id"])
        emb = _normalize(_unpack_embedding(row["embedding"]))
        by_person.setdefault(pid, []).append(emb)
    out: list[tuple[int, np.ndarray]] = []
    for pid, embs in by_person.items():
        mean = _normalize(np.mean(np.stack(embs, axis=0), axis=0))
        out.append((pid, mean))
    return out


def _match_person(
    emb: np.ndarray, centroids: list[tuple[int, np.ndarray]]
) -> int | None:
    if not centroids:
        return None
    best_id: int | None = None
    best_dist = 1e9
    for pid, cen in centroids:
        d = _cosine_distance(emb, cen)
        if d < best_dist:
            best_dist = d
            best_id = pid
    if best_id is not None and best_dist <= _AUTO_ASSIGN_DISTANCE:
        return best_id
    return None


def face_scan_preview() -> dict[str, Any]:
    with connect() as conn:
        images = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE COALESCE(media_type, 'image') = 'image'"
        ).fetchone()["c"]
        scanned = conn.execute(
            """
            SELECT COUNT(*) AS c FROM photos
            WHERE COALESCE(media_type, 'image') = 'image'
              AND face_scanned_at IS NOT NULL
            """
        ).fetchone()["c"]
        pending = conn.execute(
            """
            SELECT COUNT(*) AS c FROM photos
            WHERE COALESCE(media_type, 'image') = 'image'
              AND face_scanned_at IS NULL
            """
        ).fetchone()["c"]
        faces = conn.execute(
            "SELECT COUNT(*) AS c FROM faces WHERE ignored = 0"
        ).fetchone()["c"]
        people = conn.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]
        unnamed_count = 0
        try:
            unnamed_count = int(
                conn.execute("SELECT COUNT(*) AS c FROM face_clusters").fetchone()["c"]
            )
        except Exception:
            unnamed_count = 0
        if unnamed_count == 0:
            unnamed_count = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT cluster_key) AS c FROM faces
                    WHERE person_id IS NULL AND ignored = 0 AND cluster_key IS NOT NULL
                    """
                ).fetchone()["c"]
            )
        unassigned = conn.execute(
            """
            SELECT COUNT(*) AS c FROM faces
            WHERE person_id IS NULL AND ignored = 0
            """
        ).fetchone()["c"]
    return {
        "images": int(images),
        "scanned": int(scanned),
        "pending": int(pending),
        "faces": int(faces),
        "people": int(people),
        "unnamed_clusters": unnamed_count,
        "unassigned_faces": int(unassigned),
    }


def start_face_scan(*, confirm: bool = False, limit: int | None = None) -> dict[str, Any]:
    if not confirm:
        raise ValueError("confirm=True is required")

    from app.services.jobs import JOB_FACE_SCAN, assert_can_start

    assert_can_start(JOB_FACE_SCAN)

    with _state.lock:
        if _state.status == "running":
            raise RuntimeError("A face scan is already running")
        _state.status = "running"
        _state.phase = "starting"
        _state.total = 0
        _state.processed = 0
        _state.faces_found = 0
        _state.auto_assigned = 0
        _state.errors = 0
        _state.images_with_faces = 0
        _state.message = "Starting face scan…"
        _state.started_at = time.time()
        _state.pause_started_at = None
        _state.paused_sec = 0.0
        _state.current_photo = None
        _state.recent_faces = []
        _state.recent_photos = []
        _state.cancel_requested = False
        _state.pause_requested = False

    thread = threading.Thread(
        target=_run_face_scan,
        kwargs={"limit": limit},
        daemon=True,
    )
    thread.start()
    return get_face_scan_status()


def _finish(status: str, message: str) -> None:
    from app.services.jobs import JOB_FACE_SCAN, release_job

    with _state.lock:
        _state.status = status
        _state.phase = status
        _state.message = message
    release_job(JOB_FACE_SCAN)


def _bump_message() -> None:
    """Caller must hold _state.lock."""
    _state.message = (
        f"faces={_state.faces_found:,} auto-named={_state.auto_assigned:,} "
        f"errors={_state.errors:,} · {_state.processed:,}/{_state.total:,}"
    )


def _run_face_scan(*, limit: int | None = None) -> None:
    try:
        ensure_data_dirs()
        with _state.lock:
            _state.phase = "loading_model"
            _state.message = "Loading face model (first run may download weights)…"
        _get_face_app()

        sql = """
            SELECT id, path FROM photos
            WHERE COALESCE(media_type, 'image') = 'image'
              AND face_scanned_at IS NULL
            ORDER BY id ASC
        """
        params: list[Any] = []
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        with _state.lock:
            _state.total = len(rows)
            _state.phase = "scanning"
            _state.message = f"Scanning {len(rows):,} images…"

        centroids = _person_centroids()
        app = _get_face_app()

        def _announce_pause() -> None:
            with _state.lock:
                _state.phase = "paused"
                _state.message = "Paused — click Resume when ready"

        for row in rows:
            from app.services.job_control import wait_while_paused

            if wait_while_paused(
                lock=_state.lock,
                is_cancelled=lambda: _state.cancel_requested,
                is_paused=lambda: _state.pause_requested,
                set_paused_ui=_announce_pause,
            ):
                with _state.lock:
                    _state.current_photo = None
                _finish("cancelled", "Face scan cancelled")
                return

            with _state.lock:
                if _state.cancel_requested:
                    _state.current_photo = None
                    _finish("cancelled", "Face scan cancelled")
                    return

            photo_id = int(row["id"])
            path = Path(row["path"])
            with _state.lock:
                _state.current_photo = {
                    "id": photo_id,
                    "filename": path.name,
                }
                if not _state.pause_requested:
                    _state.phase = "scanning"
            try:
                bgr = _load_bgr(path)
                if bgr is None:
                    raise RuntimeError("Could not decode image")
                faces = app.get(bgr)
                h, w = bgr.shape[:2]
                now = _utc_now()
                inserted = 0
                assigned = 0
                new_recent: list[dict[str, Any]] = []

                with connect() as conn:
                    for face in faces:
                        bbox = face.bbox
                        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
                        fw, fh = max(0.0, x2 - x1), max(0.0, y2 - y1)
                        if fw < _MIN_FACE_PX or fh < _MIN_FACE_PX:
                            continue
                        emb = getattr(face, "embedding", None)
                        if emb is None:
                            continue
                        emb_n = _normalize(emb)
                        nx, ny = x1 / w, y1 / h
                        nw, nh = fw / w, fh / h
                        person_id = _match_person(emb_n, centroids)
                        score = float(getattr(face, "det_score", 0.0) or 0.0)
                        cur = conn.execute(
                            """
                            INSERT INTO faces(
                                photo_id, person_id, cluster_key,
                                x, y, w, h, det_score, embedding, thumb_path, ignored
                            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, 0)
                            """,
                            (
                                photo_id,
                                person_id,
                                nx,
                                ny,
                                nw,
                                nh,
                                score,
                                _pack_embedding(emb_n),
                            ),
                        )
                        face_id = int(cur.lastrowid)
                        thumb = FACES_DIR / f"{face_id}.jpg"
                        try:
                            _save_face_thumb(bgr, bbox, thumb)
                            conn.execute(
                                "UPDATE faces SET thumb_path = ? WHERE id = ?",
                                (str(thumb), face_id),
                            )
                        except Exception:
                            pass
                        inserted += 1
                        if person_id is not None:
                            assigned += 1
                        new_recent.append(
                            {
                                "id": face_id,
                                "photo_id": photo_id,
                                "person_id": person_id,
                                "det_score": round(score, 3),
                            }
                        )

                    conn.execute(
                        "UPDATE photos SET face_scanned_at = ? WHERE id = ?",
                        (now, photo_id),
                    )

                with _state.lock:
                    _state.processed += 1
                    _state.faces_found += inserted
                    _state.auto_assigned += assigned
                    if inserted:
                        _state.images_with_faces += 1
                        _state.recent_photos = [
                            {"id": photo_id, "filename": path.name, "faces": inserted},
                            *[p for p in _state.recent_photos if p.get("id") != photo_id],
                        ][:_RECENT_PHOTO_LIMIT]
                    if new_recent:
                        merged = new_recent + _state.recent_faces
                        _state.recent_faces = merged[:_RECENT_FACE_LIMIT]
                    _bump_message()

            except Exception as e:
                with connect() as conn:
                    conn.execute(
                        "UPDATE photos SET face_scanned_at = ? WHERE id = ?",
                        (_utc_now(), photo_id),
                    )
                with _state.lock:
                    _state.processed += 1
                    _state.errors += 1
                    _bump_message()
                    _state.message += f" · last error: {type(e).__name__}"

        with _state.lock:
            processed = _state.processed
            faces_found = _state.faces_found
            errors = _state.errors
            _state.current_photo = None
            _state.phase = "clustering"
            _state.message = (
                f"Scan done — clustering faces into people groups…"
            )
        try:
            cluster_result = cluster_unknown_faces(force=True)
            _finish(
                "done",
                f"Face scan complete — {processed:,} images, {faces_found:,} faces, "
                f"{errors:,} errors. {cluster_result.get('message', '')}",
            )
        except Exception as cluster_err:
            _finish(
                "done",
                f"Face scan complete — {processed:,} images, {faces_found:,} faces, "
                f"{errors:,} errors. Clustering needed: click Cluster unknowns "
                f"({cluster_err})",
            )
    except Exception as e:
        with _state.lock:
            _state.current_photo = None
        _finish("error", f"Face scan failed: {e}\n{traceback.format_exc()[-500:]}")


def cluster_unknown_faces(
    *, min_cluster_size: int = 2, force: bool = False
) -> dict[str, Any]:
    """
    Fast greedy centroid clustering of unassigned faces.
    Only groups with at least min_cluster_size faces get a cluster_key
    (so Needs a name shows real groups, not thousands of singles).
    """
    if not force:
        from app.services.jobs import get_busy_status

        busy = get_busy_status()
        if busy.get("busy"):
            raise RuntimeError(
                "Another job is running. Wait for it to finish before clustering."
            )

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, embedding FROM faces
            WHERE person_id IS NULL AND ignored = 0
            ORDER BY det_score DESC, id ASC
            """
        ).fetchall()

    if not rows:
        return {"clusters": 0, "faces": 0, "message": "No unassigned faces to cluster"}

    ids = np.asarray([int(r["id"]) for r in rows], dtype=np.int64)
    embs = np.stack(
        [_normalize(_unpack_embedding(r["embedding"])) for r in rows]
    ).astype(np.float32, copy=False)
    n, dim = embs.shape

    # Preallocate centroid matrix (worst case: every face is its own cluster)
    centroids = np.zeros((n, dim), dtype=np.float32)
    counts = np.zeros(n, dtype=np.int32)
    member_lists: list[list[int]] = []
    k = 0

    for i in range(n):
        emb = embs[i]
        face_id = int(ids[i])
        if k == 0:
            centroids[0] = emb
            counts[0] = 1
            member_lists.append([face_id])
            k = 1
            continue
        dists = 1.0 - (centroids[:k] @ emb)
        j = int(dists.argmin())
        if float(dists[j]) <= _CLUSTER_DISTANCE:
            member_lists[j].append(face_id)
            counts[j] += 1
            c = int(counts[j])
            centroids[j] = _normalize(
                centroids[j] * ((c - 1) / c) + emb / c
            )
        else:
            centroids[k] = emb
            counts[k] = 1
            member_lists.append([face_id])
            k += 1

    min_size = max(1, int(min_cluster_size))
    kept = [m for m in member_lists if len(m) >= min_size]
    singles = sum(1 for m in member_lists if len(m) < min_size)

    with connect() as conn:
        conn.execute(
            """
            UPDATE faces SET cluster_key = NULL
            WHERE person_id IS NULL AND ignored = 0
            """
        )
        for members in kept:
            key = f"c_{uuid.uuid4().hex[:12]}"
            # Chunk large IN lists for SQLite limits
            for start in range(0, len(members), 800):
                chunk = members[start : start + 800]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE faces SET cluster_key = ? WHERE id IN ({placeholders})",
                    [key, *chunk],
                )
        _rebuild_face_clusters_summary(conn)

    return {
        "clusters": len(kept),
        "faces": int(n),
        "singles_left": singles,
        "message": (
            f"Grouped {n:,} faces into {len(kept):,} people-sized clusters "
            f"({singles:,} single detections left unnamed for now)"
        ),
    }


def _rebuild_face_clusters_summary(conn) -> int:
    """Refresh face_clusters lookup table used by the People UI (fast reads)."""
    conn.execute("DELETE FROM face_clusters")
    conn.execute(
        """
        INSERT INTO face_clusters(cluster_key, face_count, photo_count, best_score, cover_face_id)
        SELECT
          cluster_key,
          COUNT(*) AS face_count,
          COUNT(DISTINCT photo_id) AS photo_count,
          MAX(det_score) AS best_score,
          MIN(id) AS cover_face_id
        FROM faces
        WHERE person_id IS NULL
          AND ignored = 0
          AND cluster_key IS NOT NULL
        GROUP BY cluster_key
        """
    )
    row = conn.execute("SELECT COUNT(*) AS c FROM face_clusters").fetchone()
    return int(row["c"])


def rebuild_face_clusters_summary() -> dict[str, Any]:
    with connect() as conn:
        total = _rebuild_face_clusters_summary(conn)
    return {
        "clusters": total,
        "message": f"Ready — {total:,} unnamed groups to review",
    }


def list_people() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.cover_face_id, p.created_at, p.updated_at,
                   COUNT(f.id) AS face_count,
                   COUNT(DISTINCT f.photo_id) AS photo_count
            FROM people p
            LEFT JOIN faces f ON f.person_id = p.id AND f.ignored = 0
            GROUP BY p.id
            ORDER BY photo_count DESC, p.name COLLATE NOCASE ASC
            """
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            cover = item.get("cover_face_id")
            if not cover:
                row = conn.execute(
                    """
                    SELECT id FROM faces
                    WHERE person_id = ? AND ignored = 0
                    ORDER BY det_score DESC, id ASC LIMIT 1
                    """,
                    (item["id"],),
                ).fetchone()
                cover = int(row["id"]) if row else None
                item["cover_face_id"] = cover
            out.append(item)
        return out


def list_clusters(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    with connect() as conn:
        # Ensure summary table exists / is populated after upgrades
        try:
            total = int(
                conn.execute("SELECT COUNT(*) AS c FROM face_clusters").fetchone()["c"]
            )
        except Exception:
            total = 0
        if total == 0:
            # Either no clusters, or summary not built yet for existing keys
            has_keys = conn.execute(
                """
                SELECT 1 AS x FROM faces
                WHERE person_id IS NULL AND ignored = 0 AND cluster_key IS NOT NULL
                LIMIT 1
                """
            ).fetchone()
            if has_keys:
                total = _rebuild_face_clusters_summary(conn)

        groups = conn.execute(
            """
            SELECT cluster_key, face_count, photo_count, best_score, cover_face_id
            FROM face_clusters
            ORDER BY face_count DESC, best_score DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        cover_ids = [int(g["cover_face_id"]) for g in groups if g["cover_face_id"]]
        covers: dict[int, dict[str, Any]] = {}
        if cover_ids:
            placeholders = ",".join("?" * len(cover_ids))
            for row in conn.execute(
                f"""
                SELECT id, photo_id, thumb_path, det_score
                FROM faces
                WHERE id IN ({placeholders})
                """,
                cover_ids,
            ).fetchall():
                covers[int(row["id"])] = dict(row)

        items = []
        for g in groups:
            cover_id = g["cover_face_id"]
            sample = covers.get(int(cover_id)) if cover_id else None
            items.append(
                {
                    "cluster_key": g["cluster_key"],
                    "face_count": int(g["face_count"]),
                    "photo_count": int(g["photo_count"]),
                    "sample_faces": [sample] if sample else [],
                }
            )
    return {"total": total, "limit": limit, "offset": offset, "clusters": items}


def get_cluster(cluster_key: str, limit: int = 48) -> dict[str, Any] | None:
    with connect() as conn:
        faces = conn.execute(
            """
            SELECT id, photo_id, thumb_path, det_score, x, y, w, h
            FROM faces
            WHERE cluster_key = ? AND ignored = 0 AND person_id IS NULL
            ORDER BY det_score DESC, id ASC
            LIMIT ?
            """,
            (cluster_key, limit),
        ).fetchall()
        if not faces:
            return None
        count = conn.execute(
            """
            SELECT COUNT(*) AS c, COUNT(DISTINCT photo_id) AS photos
            FROM faces
            WHERE cluster_key = ? AND ignored = 0 AND person_id IS NULL
            """,
            (cluster_key,),
        ).fetchone()
    return {
        "cluster_key": cluster_key,
        "face_count": int(count["c"]),
        "photo_count": int(count["photos"]),
        "faces": rows_to_dicts(list(faces)),
    }


def name_cluster(cluster_key: str, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name is required")
    if len(name) > 80:
        raise ValueError("Name is too long")

    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        face_rows = conn.execute(
            """
            SELECT id FROM faces
            WHERE cluster_key = ? AND ignored = 0 AND person_id IS NULL
            """,
            (cluster_key,),
        ).fetchall()
        if not face_rows:
            raise ValueError("Cluster not found or already named")

        if existing:
            person_id = int(existing["id"])
        else:
            cover = int(face_rows[0]["id"])
            cur = conn.execute(
                "INSERT INTO people(name, cover_face_id) VALUES (?, ?)",
                (name, cover),
            )
            person_id = int(cur.lastrowid)

        conn.execute(
            """
            UPDATE faces
            SET person_id = ?, cluster_key = NULL
            WHERE cluster_key = ? AND ignored = 0 AND person_id IS NULL
            """,
            (person_id, cluster_key),
        )
        conn.execute(
            "DELETE FROM face_clusters WHERE cluster_key = ?", (cluster_key,)
        )
        conn.execute(
            "UPDATE people SET updated_at = datetime('now') WHERE id = ?",
            (person_id,),
        )

    return {"person_id": person_id, "name": name, "faces_assigned": len(face_rows)}


def create_person(name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name is required")
    with connect() as conn:
        try:
            cur = conn.execute("INSERT INTO people(name) VALUES (?)", (name,))
            return {"id": int(cur.lastrowid), "name": name}
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise ValueError(f"Person already exists: {name}") from e
            raise


def assign_face(face_id: int, person_id: int | None, *, ignore: bool = False) -> dict[str, Any]:
    with connect() as conn:
        face = conn.execute("SELECT id FROM faces WHERE id = ?", (face_id,)).fetchone()
        if not face:
            raise ValueError("Face not found")
        if ignore:
            conn.execute(
                """
                UPDATE faces
                SET ignored = 1, person_id = NULL, cluster_key = NULL
                WHERE id = ?
                """,
                (face_id,),
            )
            return {"id": face_id, "ignored": True}
        if person_id is None:
            raise ValueError("person_id is required unless ignore=True")
        person = conn.execute(
            "SELECT id FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if not person:
            raise ValueError("Person not found")
        conn.execute(
            """
            UPDATE faces
            SET person_id = ?, cluster_key = NULL, ignored = 0
            WHERE id = ?
            """,
            (person_id, face_id),
        )
        conn.execute(
            "UPDATE people SET updated_at = datetime('now') WHERE id = ?",
            (person_id,),
        )
    return {"id": face_id, "person_id": person_id, "ignored": False}


def ignore_cluster(cluster_key: str) -> dict[str, Any]:
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE faces
            SET ignored = 1, cluster_key = NULL, person_id = NULL
            WHERE cluster_key = ? AND person_id IS NULL
            """,
            (cluster_key,),
        )
        conn.execute("DELETE FROM face_clusters WHERE cluster_key = ?", (cluster_key,))
        return {"cluster_key": cluster_key, "ignored": cur.rowcount}


def merge_people(source_id: int, target_id: int) -> dict[str, Any]:
    if source_id == target_id:
        raise ValueError("Cannot merge a person into themselves")
    with connect() as conn:
        src = conn.execute("SELECT id, name FROM people WHERE id = ?", (source_id,)).fetchone()
        tgt = conn.execute("SELECT id, name FROM people WHERE id = ?", (target_id,)).fetchone()
        if not src or not tgt:
            raise ValueError("Person not found")
        conn.execute(
            "UPDATE faces SET person_id = ? WHERE person_id = ?",
            (target_id, source_id),
        )
        conn.execute("DELETE FROM people WHERE id = ?", (source_id,))
        conn.execute(
            "UPDATE people SET updated_at = datetime('now') WHERE id = ?",
            (target_id,),
        )
    return {
        "merged_from": source_id,
        "merged_into": target_id,
        "name": tgt["name"],
    }


def rename_person(person_id: int, name: str) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name is required")
    with connect() as conn:
        try:
            cur = conn.execute(
                """
                UPDATE people SET name = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (name, person_id),
            )
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise ValueError(f"Person already exists: {name}") from e
            raise
        if cur.rowcount == 0:
            raise ValueError("Person not found")
    return {"id": person_id, "name": name}


_FIND_MATCH_DISTANCE = 0.42
_FIND_MATCH_AUTO_DISTANCE = 0.34
_MAX_REF_EMBS = 48


def find_matches_for_person(
    person_id: int,
    *,
    max_distance: float = _FIND_MATCH_DISTANCE,
    limit: int = 120,
) -> dict[str, Any]:
    """
    Compare unnamed faces to this person's known faces and return likely matches.
    Uses embeddings already in the DB — does not re-detect faces.
    """
    max_distance = float(max_distance)
    if max_distance <= 0 or max_distance > 1.0:
        raise ValueError("max_distance must be between 0 and 1")
    limit = max(1, min(int(limit), 500))

    with connect() as conn:
        person = conn.execute(
            "SELECT id, name FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if not person:
            raise ValueError("Person not found")
        ref_rows = conn.execute(
            """
            SELECT id, embedding FROM faces
            WHERE person_id = ? AND ignored = 0 AND embedding IS NOT NULL
            ORDER BY det_score DESC, id ASC
            LIMIT ?
            """,
            (person_id, _MAX_REF_EMBS),
        ).fetchall()
        if not ref_rows:
            raise ValueError("This person has no face samples yet — name a cluster first")

        candidates = conn.execute(
            """
            SELECT id, photo_id, thumb_path, det_score, embedding, cluster_key
            FROM faces
            WHERE person_id IS NULL AND ignored = 0 AND embedding IS NOT NULL
            """
        ).fetchall()

    refs = [_normalize(_unpack_embedding(r["embedding"])) for r in ref_rows]
    # Also keep a centroid for a quick first pass
    centroid = _normalize(np.mean(np.stack(refs, axis=0), axis=0))

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in candidates:
        emb = _normalize(_unpack_embedding(row["embedding"]))
        # Fast reject via centroid, then refine against individual refs
        d_cent = _cosine_distance(emb, centroid)
        if d_cent > max_distance + 0.08:
            continue
        best = min(_cosine_distance(emb, ref) for ref in refs)
        if best <= max_distance:
            scored.append(
                (
                    best,
                    {
                        "id": int(row["id"]),
                        "photo_id": int(row["photo_id"]),
                        "thumb_path": row["thumb_path"],
                        "det_score": row["det_score"],
                        "cluster_key": row["cluster_key"],
                        "distance": round(float(best), 4),
                        "auto_suggest": best <= _FIND_MATCH_AUTO_DISTANCE,
                    },
                )
            )

    scored.sort(key=lambda t: t[0])
    matches = [item for _, item in scored[:limit]]
    auto_count = sum(1 for m in matches if m["auto_suggest"])
    return {
        "person_id": person_id,
        "name": person["name"],
        "reference_faces": len(refs),
        "candidates_checked": len(candidates),
        "max_distance": max_distance,
        "match_count": len(matches),
        "auto_suggest_count": auto_count,
        "matches": matches,
        "message": (
            f"Found {len(matches):,} possible matches for {person['name']} "
            f"(checked {len(candidates):,} unnamed faces against {len(refs)} samples)"
        ),
    }


def accept_matches_for_person(person_id: int, face_ids: list[int]) -> dict[str, Any]:
    ids = sorted({int(i) for i in face_ids if int(i) > 0})
    if not ids:
        raise ValueError("No faces selected")
    with connect() as conn:
        person = conn.execute(
            "SELECT id, name FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if not person:
            raise ValueError("Person not found")
        placeholders = ",".join("?" * len(ids))
        touched = conn.execute(
            f"""
            SELECT DISTINCT cluster_key FROM faces
            WHERE id IN ({placeholders})
              AND person_id IS NULL
              AND ignored = 0
              AND cluster_key IS NOT NULL
            """,
            ids,
        ).fetchall()
        cur = conn.execute(
            f"""
            UPDATE faces
            SET person_id = ?, cluster_key = NULL, ignored = 0
            WHERE id IN ({placeholders})
              AND person_id IS NULL
              AND ignored = 0
            """,
            [person_id, *ids],
        )
        assigned = int(cur.rowcount)
        for row in touched:
            key = row["cluster_key"]
            stats = conn.execute(
                """
                SELECT COUNT(*) AS face_count,
                       COUNT(DISTINCT photo_id) AS photo_count,
                       MAX(det_score) AS best_score,
                       MIN(id) AS cover_face_id
                FROM faces
                WHERE cluster_key = ? AND person_id IS NULL AND ignored = 0
                """,
                (key,),
            ).fetchone()
            if int(stats["face_count"] or 0) <= 0:
                conn.execute("DELETE FROM face_clusters WHERE cluster_key = ?", (key,))
            else:
                conn.execute(
                    """
                    INSERT INTO face_clusters(cluster_key, face_count, photo_count, best_score, cover_face_id)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cluster_key) DO UPDATE SET
                      face_count = excluded.face_count,
                      photo_count = excluded.photo_count,
                      best_score = excluded.best_score,
                      cover_face_id = excluded.cover_face_id
                    """,
                    (
                        key,
                        int(stats["face_count"]),
                        int(stats["photo_count"]),
                        stats["best_score"],
                        stats["cover_face_id"],
                    ),
                )
        conn.execute(
            "UPDATE people SET updated_at = datetime('now') WHERE id = ?",
            (person_id,),
        )
    return {
        "person_id": person_id,
        "name": person["name"],
        "requested": len(ids),
        "assigned": assigned,
        "message": f"Added {assigned:,} faces to {person['name']}",
    }


def list_person_photos(
    person_id: int,
    limit: int = 100,
    offset: int = 0,
    sort: str | None = None,
) -> dict[str, Any] | None:
    from app.services.scanner import photo_order_sql, resolve_photo_sort

    sort_key = resolve_photo_sort(sort)
    order_by = photo_order_sql(sort_key, table_alias="p")
    with connect() as conn:
        person = conn.execute(
            "SELECT id, name, cover_face_id FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if not person:
            return None
        total = conn.execute(
            """
            SELECT COUNT(DISTINCT photo_id) AS c FROM faces
            WHERE person_id = ? AND ignored = 0
            """,
            (person_id,),
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT p.id, p.rel_path, p.filename, p.folder, p.exif_date,
                   p.width, p.height, p.media_type, p.thumb_path, p.size
            FROM photos p
            WHERE p.id IN (
              SELECT DISTINCT photo_id FROM faces
              WHERE person_id = ? AND ignored = 0
            )
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (person_id, limit, offset),
        ).fetchall()
    return {
        "id": int(person["id"]),
        "name": person["name"],
        "cover_face_id": person["cover_face_id"],
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "sort": sort_key,
        "items": strip_photo_paths(rows_to_dicts(list(rows))),
    }


def get_face(face_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
    return dict(row) if row else None


def face_thumb_path(face_id: int) -> Path | None:
    face = get_face(face_id)
    if not face or not face.get("thumb_path"):
        return None
    try:
        return assert_under_root(Path(face["thumb_path"]), FACES_DIR)
    except ValueError:
        return None
