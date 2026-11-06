from __future__ import annotations

from collections import Counter
from pathlib import Path

from app.config import THUMBS_DIR
from app.database import connect
from app.services.hashing import analyze_image, analyze_video, is_video


def main() -> None:
    mismatches = 0
    missing = 0
    fails: Counter[str] = Counter()
    fail_names: list[str] = []
    ok = 0
    checked = 0

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, filename, size, mtime FROM photos"
        ).fetchall()

    print("rows", len(rows), flush=True)
    for r in rows:
        checked += 1
        if checked % 10000 == 0:
            print(
                f"checked={checked} mismatches={mismatches} fails={sum(fails.values())}",
                flush=True,
            )
        p = Path(r["path"])
        try:
            st = p.stat()
        except OSError:
            missing += 1
            continue
        if r["size"] == st.st_size and abs(r["mtime"] - st.st_mtime) <= 0.5:
            continue
        mismatches += 1
        # Only probe first 200 mismatches (likely rehashed each scan)
        if mismatches > 200:
            continue
        try:
            thumb = THUMBS_DIR / "_probe" / f"mm_{mismatches}.jpg"
            if is_video(p):
                analyze_video(p, thumb)
            else:
                analyze_image(p, thumb)
            ok += 1
        except Exception as e:
            key = f"{p.suffix.lower()} | {type(e).__name__}: {str(e)[:160]}"
            fails[key] += 1
            if len(fail_names) < 25:
                fail_names.append(f"{r['filename']} :: {key}")
                print("FAIL", r["filename"], key, flush=True)

    print(
        "done checked",
        checked,
        "disk_missing",
        missing,
        "mtime_size_mismatch",
        mismatches,
        "probe_ok",
        ok,
        "probe_fail",
        sum(fails.values()),
        flush=True,
    )
    for k, v in fails.most_common(20):
        print(v, k, flush=True)


if __name__ == "__main__":
    main()
