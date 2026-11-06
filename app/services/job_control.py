"""Shared pause helpers for long-running jobs (between items only)."""

from __future__ import annotations

import time
from typing import Any, Callable


def wait_while_paused(
    *,
    lock,
    is_cancelled: Callable[[], bool],
    is_paused: Callable[[], bool],
    set_paused_ui: Callable[[], None] | None = None,
    poll_sec: float = 0.35,
) -> bool:
    """
    Block while paused. Returns True if the caller should abort (cancelled).
    Safe between files — does not interrupt in-flight SMB/file work.
    """
    announced = False
    while True:
        with lock:
            if is_cancelled():
                return True
            paused = is_paused()
        if not paused:
            return False
        if not announced and set_paused_ui is not None:
            set_paused_ui()
            announced = True
        time.sleep(poll_sec)


def pause_fields(paused: bool, pause_url: str | None, resume_url: str | None) -> dict[str, Any]:
    return {
        "paused": bool(paused),
        "pause": pause_url if not paused else None,
        "resume": resume_url if paused else None,
    }
