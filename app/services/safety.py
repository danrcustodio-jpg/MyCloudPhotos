"""Safety helpers for destructive actions and import sources."""

from __future__ import annotations

import os
from pathlib import Path

from app.config import get_photo_root, normalize_fs_path, photo_root_configured

# Typed confirm phrases — API rejects mismatches
CONFIRM_DELETE_ICLOUD = "DELETE FROM ICLOUD"
CONFIRM_DELETE_FOREVER = "DELETE FOREVER"
CONFIRM_CLEANUP = "DELETE FILES"


def require_confirm_token(provided: str | None, expected: str, *, action: str) -> None:
    got = (provided or "").strip()
    if got != expected:
        raise ValueError(
            f"To {action}, set confirm_token to exactly “{expected}” "
            "(UI confirmation fills this in)."
        )


def import_allowed_roots() -> list[Path]:
    """Directories under which Import may read files."""
    roots: list[Path] = []
    if photo_root_configured():
        try:
            roots.append(get_photo_root())
        except ValueError:
            pass
    home = Path.home()
    for name in ("Downloads", "Desktop", "Pictures", "Documents", "OneDrive"):
        candidate = home / name
        if candidate.is_dir():
            roots.append(candidate.resolve())
    # Optional extra prefixes via env (semicolon-separated on Windows)
    extra = os.getenv("IMPORT_ALLOW_PREFIXES", "").strip()
    if extra:
        for part in extra.replace(";", os.pathsep).split(os.pathsep):
            part = part.strip()
            if not part:
                continue
            try:
                p = normalize_fs_path(part)
                if p.is_dir():
                    roots.append(p)
            except OSError:
                continue
    # De-dupe
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def assert_import_source_allowed(source_dir: str | Path) -> Path:
    """
    Resolve source_dir and ensure it is under an allowed root
    (PHOTO_ROOT, common user folders, or IMPORT_ALLOW_PREFIXES).
    """
    src = Path(source_dir).expanduser()
    try:
        src = normalize_fs_path(src)
    except Exception:
        src = src.resolve()
    if not src.exists() or not src.is_dir():
        raise ValueError(f"Source directory not found: {src}")

    allowed = import_allowed_roots()
    if not allowed:
        raise ValueError(
            "No allowed import roots configured. Set PHOTO_ROOT or IMPORT_ALLOW_PREFIXES."
        )

    for root in allowed:
        try:
            if hasattr(src, "is_relative_to") and src.is_relative_to(root):
                return src
            src.relative_to(root)
            return src
        except ValueError:
            continue

    roots_txt = ", ".join(str(r) for r in allowed[:8])
    raise ValueError(
        f"Import source is outside allowed folders.\n"
        f"Source: {src}\n"
        f"Allowed: {roots_txt}"
        + ("…" if len(allowed) > 8 else "")
        + "\nAdd more paths with IMPORT_ALLOW_PREFIXES in .env (semicolon-separated)."
    )
