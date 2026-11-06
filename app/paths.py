from __future__ import annotations

from pathlib import Path

from app.config import normalize_fs_path


def assert_under_root(path: Path | str, root: Path | str) -> Path:
    """
    Normalize path and ensure it stays under root.
    Rejects ``..`` escapes and absolute paths outside root.
    """
    root_norm = normalize_fs_path(root)
    candidate = Path(path)

    # Reject explicit parent-directory segments before normalization
    if ".." in candidate.parts:
        raise ValueError(f"Path escapes root via '..': {path}")

    if not candidate.is_absolute():
        candidate = root_norm / candidate

    normalized = normalize_fs_path(candidate)

    try:
        if hasattr(normalized, "is_relative_to"):
            if not normalized.is_relative_to(root_norm):
                raise ValueError(f"Path is outside root: {normalized}")
        else:
            normalized.relative_to(root_norm)
    except ValueError as e:
        raise ValueError(f"Path is outside root: {normalized}") from e

    return normalized
