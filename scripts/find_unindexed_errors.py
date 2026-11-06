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
    print("indexed", len(indexed), flush=True)

    missing: list[str] = []
    missing_ext: Counter[str] = Counter()
    checked = 0

    stack = [str(root)]
    while stack and len(missing) < 1200:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not _should_skip_dir(entry.name):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    ext = Path(entry.name).suffix.lower()
                    if ext not in media_ext:
                        continue
                    checked += 1
                    if checked % 2000 == 0:
                        print(
                            f"checked={checked} missing={len(missing)} stack={len(stack)}",
                            flush=True,
                        )
                    abs_path = os.path.normpath(entry.path)
                    if abs_path not in indexed:
                        missing.append(abs_path)
                        missing_ext[ext] += 1
        except OSError as e:
            print("scandir fail", current, e, flush=True)

    print("checked_media", checked, "missing_found", len(missing), flush=True)
    print("missing_ext", dict(missing_ext.most_common(20)), flush=True)
    print("sample missing:", flush=True)
    for p in missing[:25]:
        print(" ", p, flush=True)

    # Try analyzing first 60 missing files
    fails: Counter[str] = Counter()
    ok = 0
    for i, path_str in enumerate(missing[:60], start=1):
        p = Path(path_str)
        try:
            thumb = THUMBS_DIR / "_probe" / f"miss_{i}.jpg"
            if is_video(p):
                analyze_video(p, thumb)
            else:
                analyze_image(p, thumb)
            ok += 1
        except Exception as e:
            key = f"{p.suffix.lower()} | {type(e).__name__}: {str(e)[:160]}"
            fails[key] += 1
            if sum(fails.values()) <= 20:
                print("FAIL", p.name, key, flush=True)
    print("probe_ok", ok, "probe_fail", sum(fails.values()), flush=True)
    for k, v in fails.most_common(20):
        print(v, k, flush=True)


if __name__ == "__main__":
    main()
