from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

from app.config import THUMBS_DIR, get_photo_root
from app.database import connect
from app.services.hashing import analyze_image, analyze_video, is_media, is_video


def main() -> None:
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
        print("indexed", n, flush=True)
        by = Counter()
        for r in conn.execute("SELECT filename FROM photos"):
            by[Path(r["filename"]).suffix.lower()] += 1
        print("ext", by.most_common(15), flush=True)
        null_phash = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE phash IS NULL"
        ).fetchone()["c"]
        print("null_phash", null_phash, flush=True)

    root = get_photo_root()
    print("root", root, flush=True)

    # Probe a few folders quickly (no full library walk)
    probe_dirs = []
    for name in ("FromiCloud", "Inbox"):
        p = root / name
        if p.is_dir():
            probe_dirs.append(p)
            try:
                years = [x for x in p.iterdir() if x.is_dir()][:8]
                probe_dirs.extend(years)
            except OSError as e:
                print("listdir error", p, e, flush=True)

    fails: Counter[str] = Counter()
    ok = 0
    tested = 0
    for d in probe_dirs:
        try:
            files = [f for f in d.iterdir() if f.is_file() and is_media(f)]
        except OSError as e:
            print("iterdir error", d, e, flush=True)
            continue
        print(f"probing {d.relative_to(root)} ({len(files)} media)", flush=True)
        for f in files[:40]:
            tested += 1
            try:
                thumb = THUMBS_DIR / "_probe" / f"{tested}.jpg"
                if is_video(f):
                    analyze_video(f, thumb)
                else:
                    analyze_image(f, thumb)
                ok += 1
            except Exception as e:
                key = f"{f.suffix.lower()} | {type(e).__name__}: {str(e)[:140]}"
                fails[key] += 1
                if sum(fails.values()) <= 12:
                    print("FAIL", f.name, key, flush=True)

    print("tested", tested, "ok", ok, "fail", sum(fails.values()), flush=True)
    for k, v in fails.most_common(20):
        print(v, k, flush=True)


if __name__ == "__main__":
    main()
