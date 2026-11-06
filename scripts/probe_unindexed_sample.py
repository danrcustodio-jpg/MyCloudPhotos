from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from app.config import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, THUMBS_DIR, get_photo_root
from app.database import connect
from app.services.hashing import analyze_image, analyze_video, is_video
from app.services.scanner import _should_skip_dir


def main() -> None:
    root = get_photo_root()
    media_ext = {e.lower() for e in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS}
    with connect() as conn:
        indexed = {
            os.path.normpath(r["path"])
            for r in conn.execute("SELECT path FROM photos")
        }
    print("indexed", len(indexed), "target_missing~966", flush=True)

    missing: list[str] = []
    missing_ext: Counter[str] = Counter()
    checked = 0
    stack = [str(root)]
    while stack and len(missing) < 80:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as e:
            print("scandir fail", current, e, flush=True)
            continue
        # Process files first for speed on deep trees, then dirs
        dirs = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not _should_skip_dir(entry.name):
                        dirs.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            ext = Path(entry.name).suffix.lower()
            if ext not in media_ext:
                continue
            checked += 1
            abs_path = os.path.normpath(entry.path)
            if abs_path not in indexed:
                missing.append(abs_path)
                missing_ext[ext] += 1
                if len(missing) <= 15 or len(missing) % 10 == 0:
                    print(f"missing#{len(missing)} {abs_path}", flush=True)
        # DFS into dirs (append reversed for natural order)
        stack.extend(reversed(dirs))
        if checked % 5000 == 0 and checked:
            print(f"checked={checked} missing={len(missing)}", flush=True)

    print("checked", checked, "collected_missing", len(missing), flush=True)
    print("missing_ext", dict(missing_ext), flush=True)

    fails: Counter[str] = Counter()
    ok = 0
    for i, path_str in enumerate(missing, start=1):
        p = Path(path_str)
        try:
            size = p.stat().st_size
        except OSError as e:
            fails[f"stat | {type(e).__name__}: {e}"] += 1
            continue
        try:
            thumb = THUMBS_DIR / "_probe" / f"u_{i}.jpg"
            if is_video(p):
                analyze_video(p, thumb)
            else:
                analyze_image(p, thumb)
            ok += 1
            print(f"unexpected OK {p.name} size={size}", flush=True)
        except Exception as e:
            key = f"{p.suffix.lower()} size={size} | {type(e).__name__}: {str(e)[:180]}"
            fails[key] += 1
            if sum(fails.values()) <= 25:
                print("FAIL", p.name, key, flush=True)

    print("probe_ok", ok, "probe_fail", sum(fails.values()), flush=True)
    for k, v in fails.most_common(25):
        print(v, k, flush=True)


if __name__ == "__main__":
    main()
