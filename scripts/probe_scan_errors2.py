from __future__ import annotations

from collections import Counter
from pathlib import Path

from app.config import THUMBS_DIR, get_photo_root
from app.database import connect
from app.services.hashing import analyze_image, analyze_video, is_video


def main() -> None:
    fails: Counter[str] = Counter()
    ok = 0
    tested = 0
    missing = 0

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT path, filename FROM photos
            WHERE lower(filename) LIKE '%.heic'
               OR lower(filename) LIKE '%.png'
               OR lower(filename) LIKE '%.gif'
            ORDER BY indexed_at DESC
            LIMIT 100
            """
        ).fetchall()
        rows2 = conn.execute(
            """
            SELECT path, filename FROM photos
            WHERE lower(filename) LIKE '%.jpg' OR lower(filename) LIKE '%.jpeg'
            ORDER BY indexed_at DESC
            LIMIT 40
            """
        ).fetchall()

    print("sample_size", len(rows) + len(rows2), flush=True)
    for r in list(rows) + list(rows2):
        p = Path(r["path"])
        tested += 1
        try:
            if not p.exists():
                missing += 1
                fails["MISSING on disk"] += 1
                continue
            thumb = THUMBS_DIR / "_probe" / f"{tested}.jpg"
            if is_video(p):
                analyze_video(p, thumb)
            else:
                analyze_image(p, thumb)
            ok += 1
        except Exception as e:
            key = f"{p.suffix.lower()} | {type(e).__name__}: {str(e)[:140]}"
            fails[key] += 1
            if sum(v for k, v in fails.items() if k != "MISSING on disk") <= 15:
                print("FAIL", r["filename"], key, flush=True)

    print("tested", tested, "ok", ok, "missing", missing, "fail", sum(fails.values()), flush=True)
    for k, v in fails.most_common(25):
        print(v, k, flush=True)

    # Also: compare folder counts from DB vs quick sample of one high-count folder
    with connect() as conn:
        top = conn.execute(
            """
            SELECT folder, COUNT(*) AS c FROM photos
            GROUP BY folder ORDER BY c DESC LIMIT 5
            """
        ).fetchall()
    print("top folders:", flush=True)
    for f in top:
        print(f["c"], repr(f["folder"]), flush=True)


if __name__ == "__main__":
    main()
