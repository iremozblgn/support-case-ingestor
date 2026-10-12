from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any


DEBUG_DIR = Path("debug")
PROTECTED_DEBUG_PATHS = {
    DEBUG_DIR / "browser_request_full.json",
    DEBUG_DIR / "browser_message.json",
}
PROTECTED_DEBUG_DIR_NAMES = {"playwright_profile"}


def should_write_debug_artifacts(settings: Any, *, failure: bool = False) -> bool:
    return bool(failure or getattr(settings, "debug_artifacts_enabled", False))


def capture_page_artifacts(
    page: Any,
    *,
    screenshot_path: Path,
    html_path: Path,
    settings: Any | None = None,
    failure: bool = False,
) -> tuple[str | None, str | None]:
    if settings is not None and not should_write_debug_artifacts(settings, failure=failure):
        return None, None

    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    captured_screenshot_path: str | None = None
    captured_html_path: str | None = None

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        captured_screenshot_path = str(screenshot_path)
    except Exception:
        captured_screenshot_path = None

    try:
        html_path.write_text(page.content(), encoding="utf-8")
        captured_html_path = str(html_path)
    except Exception:
        captured_html_path = None

    return captured_screenshot_path, captured_html_path


def cleanup_debug_artifacts(settings: Any, logger: logging.Logger | None = None) -> None:
    local_logger = logger or logging.getLogger(__name__)
    retention_days = max(0, int(getattr(settings, "debug_artifact_retention_days", 3) or 0))
    cutoff = time.time() - (retention_days * 86400)
    if not DEBUG_DIR.exists():
        return

    for path in DEBUG_DIR.iterdir():
        if path.name in PROTECTED_DEBUG_DIR_NAMES:
            continue
        if path in PROTECTED_DEBUG_PATHS:
            continue
        if path.is_dir():
            continue
        if path.suffix.lower() not in {".png", ".html"}:
            continue

        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue
        except Exception:
            local_logger.debug("Failed to delete debug artifact %s", path, exc_info=True)
